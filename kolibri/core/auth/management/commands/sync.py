import json
import logging
import math

from django.core.management import call_command
from django.core.management.base import CommandError
from morango.models import Filter
from morango.models import InstanceIDModel
from morango.models import ScopeDefinition
from morango.sync.controller import MorangoProfileController

from ..utils import bytes_for_humans
from ..utils import create_superuser_and_provision_device
from ..utils import get_baseurl
from ..utils import get_client_and_server_certs
from ..utils import get_dataset_id
from kolibri.core.auth.constants.morango_sync import PROFILE_FACILITY_DATA
from kolibri.core.auth.constants.morango_sync import ScopeDefinitions
from kolibri.core.auth.constants.morango_sync import State
from kolibri.core.auth.management.utils import get_facility
from kolibri.core.auth.management.utils import run_once
from kolibri.core.tasks.management.commands.base import AsyncCommand
from kolibri.core.tasks.utils import db_task_write_lock
from kolibri.utils import conf

DATA_PORTAL_SYNCING_BASE_URL = conf.OPTIONS["Urls"]["DATA_PORTAL_SYNCING_BASE_URL"]


logger = logging.getLogger(__name__)


class Command(AsyncCommand):
    help = "Allow the syncing of facility data with Kolibri Data Portal or another Kolibri device."

    def add_arguments(self, parser):
        parser.add_argument(
            "--facility", action="store", type=str, help="ID of facility to sync"
        )
        parser.add_argument(
            "--baseurl", type=str, default=DATA_PORTAL_SYNCING_BASE_URL, dest="baseurl"
        )
        parser.add_argument("--noninteractive", action="store_true")
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=500,
            help="Chunk size of records to send/retrieve per request",
        )
        parser.add_argument(
            "--no-push", action="store_true", help="Do not push data to the server"
        )
        parser.add_argument(
            "--no-pull", action="store_true", help="Do not pull data from the server"
        )
        parser.add_argument(
            "--username",
            type=str,
            help="username of superuser on server we are syncing with",
        )
        parser.add_argument(
            "--password",
            type=str,
            help="password of superuser on server we are syncing with",
        )
        # parser.add_argument("--scope-id", type=str, default=FULL_FACILITY)

    def handle_async(self, *args, **options):

        (
            baseurl,
            facility_id,
            chunk_size,
            username,
            password,
            no_push,
            no_pull,
            noninteractive,
        ) = (
            options["baseurl"],
            options["facility"],
            options["chunk_size"],
            options["username"],
            options["password"],
            options["no_push"],
            options["no_pull"],
            options["noninteractive"],
        )

        PORTAL_SYNC = baseurl == DATA_PORTAL_SYNCING_BASE_URL

        # validate url that is passed in
        if not PORTAL_SYNC:
            baseurl = get_baseurl(baseurl)

        # call this in case user directly syncs without migrating database
        if not ScopeDefinition.objects.filter():
            call_command("loaddata", "scopedefinitions")

        # try to connect to server
        controller = MorangoProfileController(PROFILE_FACILITY_DATA)
        network_connection = controller.create_network_connection(baseurl)

        # if instance_ids are equal, this means device is trying to sync with itself, which we don't allow
        if (
            InstanceIDModel.get_or_create_current_instance()[0].id
            == network_connection.server_info["instance_id"]
        ):
            raise CommandError(
                "Device can not sync with itself. Please recheck base URL and try again."
            )

        if PORTAL_SYNC:  # do portal sync setup
            facility = get_facility(
                facility_id=facility_id, noninteractive=noninteractive
            )

            # check for the certs we own for the specific facility
            client_cert = (
                facility.dataset.get_owned_certificates()
                .filter(scope_definition_id=ScopeDefinitions.FULL_FACILITY)
                .first()
            )
            if not client_cert:
                raise CommandError(
                    "This device does not own a certificate for Facility: {}".format(
                        facility.name
                    )
                )

            # get primary partition
            scope_params = json.loads(client_cert.scope_params)
            dataset_id = scope_params["dataset_id"]

            # check if the server already has a cert for this facility
            server_certs = network_connection.get_remote_certificates(
                dataset_id, scope_def_id=ScopeDefinitions.FULL_FACILITY
            )

            # if necessary, push a cert up to the server
            server_cert = (
                server_certs[0]
                if server_certs
                else network_connection.push_signed_client_certificate_chain(
                    local_parent_cert=client_cert,
                    scope_definition_id=ScopeDefinitions.FULL_FACILITY,
                    scope_params=scope_params,
                )
            )

        else:  # do P2P setup
            dataset_id = get_dataset_id(
                baseurl, identifier=facility_id, noninteractive=noninteractive
            )

            client_cert, server_cert, username = get_client_and_server_certs(
                username,
                password,
                dataset_id,
                network_connection,
                noninteractive=noninteractive,
            )

        logger.info("Syncing has been initiated (this may take a while)...")

        sync_client = network_connection.create_sync_session(
            client_cert, server_cert, chunk_size=chunk_size
        )

        # setup all progress trackers before starting so CLI progress is accurate
        self._setup_progress_tracking(
            sync_client, not no_pull, not no_push, noninteractive
        )

        # pull from server and push our own data to server
        if not no_pull:
            self._session_tracker_adapter(
                sync_client.signals.session,
                "Creating pull transfer session",
                "Completed pull transfer session",
            )
            with db_task_write_lock:
                sync_client.initiate_pull(Filter(dataset_id))
        if not no_push:
            self._session_tracker_adapter(
                sync_client.signals.session,
                "Creating push transfer session",
                "Completed push transfer session",
            )
            with db_task_write_lock:
                sync_client.initiate_push(Filter(dataset_id))

        with db_task_write_lock:
            create_superuser_and_provision_device(
                username, dataset_id, noninteractive=noninteractive
            )
        sync_client.close_sync_session()

        if self.job:
            self.job.extra_metadata.update(sync_state=State.COMPLETED)

        logger.info("Syncing has been completed.")

    def _setup_progress_tracking(self, sync_client, pulling, pushing, noninteractive):
        """
        Sets up progress trackers for the various sync stages

        :type sync_client: morango.sync.syncsession.SyncClient
        :type pulling: bool
        :type pushing: bool
        :type noninteractive: bool
        """
        transfer_message = "{records_transferred}/{records_total}, {transfer_total}"

        if pulling:
            self._queueing_tracker_adapter(
                sync_client.signals.queuing,
                "Remotely preparing data",
                State.REMOTE_QUEUING,
                False,
                noninteractive,
            )
            self._transfer_tracker_adapter(
                sync_client.signals.pulling,
                "Receiving data ({})".format(transfer_message),
                State.PULLING,
                noninteractive,
            )
            self._queueing_tracker_adapter(
                sync_client.signals.dequeuing,
                "Locally integrating received data",
                State.LOCAL_DEQUEUING,
                False,
                noninteractive,
            )

        if pushing:
            self._queueing_tracker_adapter(
                sync_client.signals.queuing,
                "Locally preparing data to send",
                State.LOCAL_QUEUING,
                True,
                noninteractive,
            )
            self._transfer_tracker_adapter(
                sync_client.signals.pushing,
                "Sending data ({})".format(transfer_message),
                State.PUSHING,
                noninteractive,
            )
            self._queueing_tracker_adapter(
                sync_client.signals.dequeuing,
                "Remotely integrating data",
                State.REMOTE_DEQUEUING,
                True,
                noninteractive,
            )

    def _update_all_progress(self, progress_fraction, progress):
        """
        Override parent progress update callback to report from all of our progress trackers
        """
        total_progress = sum([p.progress for p in self.progresstrackers])
        total = sum([p.total for p in self.progresstrackers])
        progress_fraction = total_progress / float(total) if total > 0.0 else 0.0

        if self.job:
            self.job.update_progress(progress_fraction, 1.0)
            self.job.extra_metadata.update(progress.extra_data)
            self.job.save_meta()

    def _session_tracker_adapter(self, signal_group, started_msg, completed_msg):
        """
        Attaches a signal handler to session creation signals

        :type signal_group: morango.sync.syncsession.SyncSignalGroup
        :type started_msg: str
        :type completed_msg: str
        """

        @run_once
        def session_creation(transfer_session):
            """
            A session is created individually for pushing and pulling
            """
            logger.info(started_msg)
            if self.job:
                self.job.extra_metadata.update(sync_state=State.SESSION_CREATION)

        @run_once
        def session_destruction(transfer_session):
            if transfer_session.records_total == 0:
                logger.info("There are no records to transfer")
            logger.info(completed_msg)

        signal_group.started.connect(session_creation)
        signal_group.completed.connect(session_destruction)

    def _transfer_tracker_adapter(
        self, signal_group, message, sync_state, noninteractive
    ):
        """
        Attaches a signal handler to pushing/pulling signals

        :type signal_group: morango.sync.syncsession.SyncSignalGroup
        :type message: str
        :type sync_state: str
        :type noninteractive: bool
        """
        tracker = self.start_progress(total=100)

        def stats_msg(transfer_session):
            transfer_total = (
                transfer_session.bytes_sent + transfer_session.bytes_received
            )
            return message.format(
                records_transferred=transfer_session.records_transferred,
                records_total=transfer_session.records_total,
                transfer_total=bytes_for_humans(transfer_total),
            )

        def stats(transfer_session):
            logger.info(stats_msg(transfer_session))

        def handler(transfer_session):
            """
            :type transfer_session: morango.models.core.TransferSession
            """
            progress = (
                100
                * transfer_session.records_transferred
                / float(transfer_session.records_total)
            )
            tracker.update_progress(
                increment=math.ceil(progress - tracker.progress),
                message=stats_msg(transfer_session),
                extra_data=dict(
                    bytes_sent=transfer_session.bytes_sent,
                    bytes_received=transfer_session.bytes_received,
                    sync_state=sync_state,
                ),
            )

        signal_group.started.connect(stats)
        signal_group.connect(handler)

        if noninteractive or tracker.progressbar is None:
            signal_group.in_progress.connect(stats)

        # log one more time at end to capture in logging output
        signal_group.completed.connect(stats)

    def _queueing_tracker_adapter(
        self, signal_group, message, sync_state, is_push, noninteractive
    ):
        """
        Attaches a signal handler to queuing/dequeuing signals, filtered by `is_local` on
        whether or not to match a local or remote signal

        :type signal_group: morango.sync.syncsession.SyncSignalGroup
        :type message: str
        :type sync_state: str
        :type is_push: bool
        :type noninteractive: bool
        """
        tracker = self.start_progress(total=2)

        def started(transfer_session):
            if transfer_session.push == is_push:
                logger.info(message)

        def handler(transfer_session):
            if transfer_session.push == is_push:
                tracker.update_progress(
                    message=message, extra_data=dict(sync_state=sync_state)
                )

        if noninteractive or tracker.progressbar is None:
            signal_group.started.connect(started)

        signal_group.started.connect(handler)
        signal_group.completed.connect(handler)
