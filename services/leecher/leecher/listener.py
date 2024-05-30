"""
A Shotgrid Events listener leecher for Ayon.

This service will continually run and query the EventLogEntry table from
Shotgrid and converts them to Ayon events, and can be configured from the Ayon
Addon settings page.
"""
from pprint import pformat
import sys
import time
import signal
import copy
import socket
import traceback
from typing import Any, Callable, Union

from utils import get_logger

from constants import (
    SG_EVENT_TYPES,
    SG_EVENT_QUERY_FIELDS,
)

import ayon_api
import shotgun_api3


class ShotgridListener:
    log = get_logger(__file__)

    def __init__(self, func: Union[Callable, None] = None):
        """Ensure both Ayon and Shotgrid connections are available.

        Set up common needed attributes and handle shotgrid connection
        closure via signal handlers.

        Args:
            func (Callable, None): In case we want to override the default
                function we cast to the processed events.
        """
        self.log.info("Initializing the Shotgrid Listener.")
        if func is None:
            self.func = self.send_shotgrid_event_to_ayon
        else:
            self.func = func

        try:
            ayon_api.init_service()
            self.settings = ayon_api.get_service_addon_settings()
            service_settings = self.settings["service_settings"]

            self.sg_url = self.settings["shotgrid_server"]
            self.sg_project_code_field = self.settings[
                "shotgrid_project_code_field"]

            # get server op related ShotGrid script api properties
            shotgrid_secret = ayon_api.get_secret(
                service_settings["script_key"])

            if isinstance(shotgrid_secret, list):
                raise ValueError(
                    "Shotgrid API Key not found. Make sure to set it in the "
                    "Addon System settings. "
                    "`ayon+settings://shotgrid/service_settings/script_key`"
                )

            self.sg_api_key = shotgrid_secret.get("value")
            if not self.sg_api_key:
                raise ValueError(
                    "Shotgrid API Key not found. Make sure to set it in the "
                    "Addon System settings."
                )

            self.sg_script_name = service_settings["script_name"]
            if not self.sg_script_name:
                raise ValueError(
                    "Shotgrid Script Name not found. Make sure to set it in "
                    "the Addon System settings."
                )

            self.custom_attribs_map = {
                attr["ayon"]: attr["sg"]
                for attr in self.settings["compatibility_settings"]["custom_attribs_map"]  # noqa: E501
                if attr["sg"]
            }

            # TODO: implement a way to handle status_list and tags
            self.custom_attribs_map.update({
                # "status": "status_list",
                "tags": "tags"
            })

            self.sg_enabled_entities = self.settings["compatibility_settings"]["shotgrid_enabled_entities"]  # noqa: E501

            try:
                self.shotgrid_polling_frequency = int(
                    service_settings["polling_frequency"]
                )
            except Exception:
                self.shotgrid_polling_frequency = 10

        except Exception as e:
            self.log.error(
                "Unable to get Addon settings from the server.")
            raise e

        try:
            self.sg_session = shotgun_api3.Shotgun(
                self.sg_url,
                script_name=self.sg_script_name,
                api_key=self.sg_api_key
            )
            self.sg_session.connect()
        except Exception as e:
            self.log.error("Unable to connect to Shotgrid Instance:")
            raise e

        signal.signal(signal.SIGINT, self._signal_teardown_handler)
        signal.signal(signal.SIGTERM, self._signal_teardown_handler)

    def _signal_teardown_handler(self, signalnum, frame):
        self.log.warning("Process stop requested. Terminating process.")
        self.sg_session.close()
        self.log.warning("Termination finished.")
        sys.exit(0)

    def _build_shotgrid_filters(self):
        """Build SG filters for Events query.

        We want to filter out all the Events in the SG database that do not
        meet our needs:
            1) Events of Projects with "AYON Auto Sync" enabled.
            2) Events on entities and type for entities we track.

        Returns:
            filters (list): Filter to apply to the SG query.
        """
        filters = []

        sg_projects = self.sg_session.find(
            "Project", filters=[["sg_ayon_auto_sync", "is", True]]
        )

        if not sg_projects:
            return []

        filters.append(["project", "in", sg_projects])

        if sg_event_types := self._get_supported_event_types():
            filters.append(["event_type", "in", sg_event_types])

        return filters

    def _get_supported_event_types(self) -> list[str]:
        sg_event_types = []
        for entity_type in self.sg_enabled_entities:
            sg_event_types.extend(
                event_name.format(entity_type) for event_name in SG_EVENT_TYPES
            )
        return sg_event_types

    def _get_last_event_processed(self, sg_filters):
        """Find the Event ID for the last SG processed event.

        First attempt to find it via AYON, if none is found we get the last
        matching event from Shotgrid.

        Returns:
            last_event_id (int): The last known Event id.
        """
        last_event_id = None

        for last_event_id in ayon_api.get_events(
            topics=["shotgrid.leech"], fields=["hash"]
        ):
            last_event_id = int(last_event_id["hash"])

        if not last_event_id:
            last_event = self.sg_session.find_one(
                "EventLogEntry",
                filters=sg_filters,
                fields=["id", "project"],
                order=[{"column": "id", "direction": "desc"}],
            )
            last_event_id = last_event["id"]

        return last_event_id

    def start_listening(self):
        """Main loop querying the Shotgrid database for new events

        Since Shotgrid does not have an event hub per se, we need to query
        the "EventLogEntry table and send these as Ayon events for processing.

        We try to continue from the last Event processed by the leecher, if
        none is found we start at the moment in time.
        """
        self.log.info("Start listening for Shotgrid Events...")

        base_sg_filters = self._build_shotgrid_filters()
        supported_event_types = self._get_supported_event_types()
        last_event_id = self._get_last_event_processed(base_sg_filters)

        while True:
            if not base_sg_filters:
                self.log.debug(
                    f"0 Leecher waiting {self.shotgrid_polling_frequency} "
                    "seconds..."
                )
                time.sleep(self.shotgrid_polling_frequency)
                base_sg_filters = self._build_shotgrid_filters()
                continue

            # try to get the last event processed in case we lost it due to
            # a crash or restart
            if not last_event_id:
                last_event_id = self._get_last_event_processed(base_sg_filters)

            # and add the last event processed as a filter
            sg_filters = copy.deepcopy(base_sg_filters)
            sg_filters.append(["id", "greater_than", last_event_id])

            try:
                events = self.sg_session.find(
                    "EventLogEntry",
                    sg_filters,
                    SG_EVENT_QUERY_FIELDS,
                    order=[{"column": "id", "direction": "asc"}],
                    limit=50,
                )

                if not events:
                    self.log.debug(
                        f"1 Leecher waiting {self.shotgrid_polling_frequency} "
                        "seconds..."
                    )
                    time.sleep(self.shotgrid_polling_frequency)
                    continue

                self.log.info(f"Found {len(events)} events in Shotgrid.")

                for event in events:
                    if not event:
                        continue

                    if (
                        event["event_type"].endswith("_Change")
                        and event["attribute_name"].replace("sg_", "") in list(self.custom_attribs_map.values())  # noqa: E501
                    ):
                        # events related to custom attributes changes
                        # check if event was caused by api user
                        if not self._is_api_user_event(event):
                            last_event_id = event.get("id", None)
                        else:
                            last_event_id = None

                    elif event["event_type"] in supported_event_types:
                        # events related to changes in entities we track
                        # check if event was caused by api user
                        if not self._is_api_user_event(event):
                            last_event_id = event.get("id", None)
                        else:
                            last_event_id = None

                    if not last_event_id:
                        self.log.info(f"Ignoring event: {pformat(event)}")
                        continue

                    last_event_id = self.func(event)

            except Exception:
                self.log.error(traceback.format_exc())

            self.log.debug(
                f"2 Leecher waiting {self.shotgrid_polling_frequency} seconds..."
            )
            time.sleep(self.shotgrid_polling_frequency)
            continue

    def _is_api_user_event(self, event: dict[str, Any]) -> bool:
        """Check if the event was caused by an API user.

        Args:
            event (dict): The Shotgrid Event data.

        Returns:
            bool: True if the event was caused by an API user.
        """
        # TODO: we have to create specific api user filtering
        if (
            event.get("meta", {}).get("sudo_actual_user", {}).get("type")
            == "ApiUser"
        ):
            return True

    def send_shotgrid_event_to_ayon(self, payload: dict[str, Any]) -> int:
        """Send the Shotgrid event as an Ayon event.

        Args:
            payload (dict): The Event data.

        Returns:
            int: The Shotgrid Event ID.
        """
        description = f"Leeched {payload['event_type']}"
        user_name = payload.get("user", {}).get("name", "Undefined")

        if user_name:
            description = f"Leeched {payload['event_type']} by {user_name}"

        # fix non serializable datetime
        payload["created_at"] = payload["created_at"].isoformat()

        if payload.get("meta", {}).get("entity_type", "Undefined") == "Project":
            project_name = payload.get("entity", {}).get("name", "Undefined")
            project_id = payload.get("entity", {}).get("id", "Undefined")
        else:
            project_name = payload.get("project", {}).get("name", "Undefined")
            project_id = payload.get("project", {}).get("id", "Undefined")

        sg_project = self.sg_session.find_one(
            "Project", [["id", "is", project_id]], fields=[self.sg_project_code_field]
        )

        ayon_api.dispatch_event(
            "shotgrid.event",
            sender=socket.gethostname(),
            event_hash=payload["id"],
            project_name=project_name,
            username=user_name,
            description=description,
            summary=None,
            payload={
                "action": "shotgrid-event",
                "user_name": user_name,
                "project_name": project_name,
                "project_code": sg_project.get(self.sg_project_code_field),
                "project_code_field": self.sg_project_code_field,
                "sg_payload": payload,
            },
        )

        self.log.info("Dispatched Ayon event with payload:", payload)

        return payload["id"]
