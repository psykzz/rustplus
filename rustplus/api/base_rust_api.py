import asyncio
from typing import List, Callable, Union
from PIL import Image

from .remote.events.event_loop_manager import EventLoopManager
from .structures import *
from .remote.rustplus_proto import AppEmpty, AppRequest
from .remote import RustRemote, HeartBeat, MapEventListener, ServerChecker, RateLimiter
from .remote.camera import CameraManager
from ..commands import CommandOptions, CommandHandler
from ..commands.command_data import CommandData
from ..exceptions import *
from .remote.events import (
    RegisteredListener,
    EntityEvent,
    TeamEvent,
    ChatEvent,
    ProtobufEvent,
)
from ..utils import deprecated
from ..conversation import ConversationFactory
from ..utils import ServerID


class BaseRustSocket:
    def __init__(
        self,
        ip: str = None,
        port: str = None,
        steam_id: int = None,
        player_token: int = None,
        command_options: CommandOptions = None,
        raise_ratelimit_exception: bool = False,
        ratelimit_limit: int = 25,
        ratelimit_refill: int = 3,
        heartbeat: HeartBeat = None,
        use_proxy: bool = False,
        use_test_server: bool = False,
        event_loop: asyncio.AbstractEventLoop = None,
        rate_limiter: RateLimiter = None,
    ) -> None:

        if ip is None:
            raise ValueError("Ip cannot be None")
        if steam_id is None:
            raise ValueError("SteamID cannot be None")
        if player_token is None:
            raise ValueError("PlayerToken cannot be None")

        self.server_id = ServerID(ip, port, steam_id, player_token)
        self.seq = 1
        self.command_options = command_options
        self.raise_ratelimit_exception = raise_ratelimit_exception
        self.ratelimit_limit = ratelimit_limit
        self.ratelimit_refill = ratelimit_refill
        self.marker_listener = MapEventListener(self)
        self.use_test_server = use_test_server
        self.event_loop = event_loop

        self.remote = RustRemote(
            server_id=self.server_id,
            command_options=command_options,
            ratelimit_limit=ratelimit_limit,
            ratelimit_refill=ratelimit_refill,
            use_proxy=use_proxy,
            api=self,
            use_test_server=use_test_server,
            rate_limiter=rate_limiter,
        )

        if heartbeat is None:
            raise ValueError("Heartbeat cannot be None")
        self.heartbeat = heartbeat

    async def _handle_ratelimit(self, amount=1) -> None:
        """
        Handles the ratelimit for a specific request. Will sleep if tokens are not currently available and is set to wait
        :param amount: The amount to consume
        :raises RateLimitError - If the tokens are not available and is not set to wait
        :return: None
        """
        while True:

            if self.remote.ratelimiter.can_consume(self.server_id, amount):
                self.remote.ratelimiter.consume(self.server_id, amount)
                break

            if self.raise_ratelimit_exception:
                raise RateLimitError("Out of tokens")

            await asyncio.sleep(
                self.remote.ratelimiter.get_estimated_delay_time(self.server_id, amount)
            )

        self.heartbeat.reset_rhythm()

    def _generate_protobuf(self) -> AppRequest:
        """
        Generates the default protobuf for a request

        :return: AppRequest - The default request object
        """
        app_request = AppRequest()
        app_request.seq = self.seq
        app_request.playerId = self.server_id.player_id
        app_request.playerToken = self.server_id.player_token

        self.seq += 1

        return app_request

    async def connect(
        self, retries: int = float("inf"), delay: int = 20, on_failure=None
    ) -> None:
        """
        Attempts to open a connection to the rust game server specified in the constructor

        :return: None
        """
        EventLoopManager.set_loop(
            self.event_loop
            if self.event_loop is not None
            else asyncio.get_event_loop(),
            self.server_id,
        )

        if not self.use_test_server:
            ServerChecker(self.server_id.ip, self.server_id.port).run()

        EventLoopManager.set_loop(
            self.event_loop
            if self.event_loop is not None
            else asyncio.get_event_loop(),
            self.server_id,
        )

        try:
            if self.remote.ws is None:
                await self.remote.connect(
                    retries=retries,
                    delay=delay,
                    on_failure=on_failure,
                )
                await self.heartbeat.start_beat()
        except ConnectionRefusedError:
            raise ServerNotResponsiveError("Cannot Connect")

    async def close_connection(self) -> None:
        """
        Disconnects from the Rust Server

        :return: None
        """
        self.remote.close()

    async def disconnect(self) -> None:
        """
        Disconnects from the Rust Server

        :return: None
        """
        await self.close_connection()

    async def send_wakeup_request(self) -> None:
        """
        Sends a request to the server to wake up broadcast responses

        :return: None
        """
        await self._handle_ratelimit()

        app_request = self._generate_protobuf()
        app_request.getTime.CopyFrom(AppEmpty())

        self.remote.ignored_responses.append(app_request.seq)

        await self.remote.send_message(app_request)

    async def switch_server(
        self,
        ip: str = None,
        port: str = None,
        steam_id: int = None,
        player_token: int = None,
        command_options: CommandOptions = None,
        raise_ratelimit_exception: bool = True,
        connect: bool = False,
        use_proxy: bool = False,
    ) -> None:
        """
        Disconnects and replaces server params, allowing the socket to connect to a new server.

        :param raise_ratelimit_exception: Whether to raise an exception or wait
        :param command_options: The command options
        :param ip: IP of the server
        :param port: Port of the server
        :param player_token: The player Token
        :param steam_id: Steam id of the player
        :param connect: bool indicating if socket should automatically self.connect()
        :param use_proxy: Whether to use the facepunch proxy
        :return: None
        """

        if self.use_test_server:
            raise ServerSwitchDisallowedError("Cannot switch server")

        if ip is None:
            raise ValueError("Ip cannot be None")
        if port is None:
            raise ValueError("Port cannot be None")
        if steam_id is None:
            raise ValueError("SteamID cannot be None")
        if player_token is None:
            raise ValueError("PlayerToken cannot be None")

        # disconnect before redefining
        await self.disconnect()

        # Reset basic credentials
        self.server_id = ServerID(ip, port, steam_id, player_token)
        self.seq = 1

        # Deal with commands

        if command_options is not None:
            self.command_options = command_options
            self.remote.command_options = command_options
            if self.remote.use_commands:
                self.remote.command_handler.command_options = command_options
            else:
                self.remote.use_commands = True
                self.remote.command_handler = CommandHandler(self.command_options, self)

        self.raise_ratelimit_exception = raise_ratelimit_exception

        self.remote.pending_entity_subscriptions = []
        self.remote.server_id = ServerID(ip, port, steam_id, player_token)

        # reset ratelimiter
        self.remote.ratelimiter.remove(self.server_id)
        self.remote.ratelimiter.add_socket(
            self.server_id,
            self.ratelimit_limit,
            self.ratelimit_limit,
            1,
            self.ratelimit_refill,
        )
        self.remote.conversation_factory = ConversationFactory(self)
        # remove entity events
        EntityEvent.handlers.unregister_all()
        # reset marker listener
        self.marker_listener.persistent_ids.clear()
        self.marker_listener.highest_id = 0

        if connect:
            await self.connect()

    def command(
        self,
        coro: Callable = None,
        aliases: List[str] = None,
        alias_func: Callable = None,
    ) -> Union[Callable, RegisteredListener]:
        """
        A coroutine decorator used to register a command executor

        :param alias_func: The function to test the aliases against
        :param aliases: The aliases to register the command under
        :param coro: The coroutine to call when the command is called
        :return: RegisteredListener - The listener object | Callable - The callable func for the decorator
        """

        if isinstance(coro, RegisteredListener):
            coro = coro.get_coro()

        if asyncio.iscoroutinefunction(coro):
            cmd_data = CommandData(
                coro,
                aliases,
                alias_func,
            )
            self.remote.command_handler.register_command(cmd_data)
            return RegisteredListener(coro.__name__, cmd_data.coro)

        def wrap_func(coro):

            if self.command_options is None:
                raise CommandsNotEnabledError("Not enabled")

            if isinstance(coro, RegisteredListener):
                coro = coro.get_coro()

            cmd_data = CommandData(
                coro,
                aliases,
                alias_func,
            )
            self.remote.command_handler.register_command(cmd_data)
            return RegisteredListener(coro.__name__, cmd_data.coro)

        return wrap_func

    def team_event(self, coro) -> RegisteredListener:
        """
        A Decorator to register an event listener for team changes

        :param coro: The coroutine to call when a change happens
        :return: RegisteredListener - The listener object
        """

        if isinstance(coro, RegisteredListener):
            coro = coro.get_coro()

        listener = RegisteredListener("team_changed", coro)
        TeamEvent.handlers.register(listener, self.server_id)
        return listener

    def chat_event(self, coro) -> RegisteredListener:
        """
        A Decorator to register an event listener for chat messages

        :param coro: The coroutine to call when a message is sent
        :return: RegisteredListener - The listener object
        """

        if isinstance(coro, RegisteredListener):
            coro = coro.get_coro()

        listener = RegisteredListener("chat_message", coro)
        ChatEvent.handlers.register(listener, self.server_id)
        return listener

    def entity_event(self, eid):
        """
        Decorator to register a smart device listener

        :param eid: The entity id of the entity
        :return: RegisteredListener - The listener object
        :raises SmartDeviceRegistrationError
        """

        def wrap_func(coro) -> RegisteredListener:

            if isinstance(coro, RegisteredListener):
                coro = coro.get_coro()

            self.remote.handle_subscribing_entity(eid, coro)

            return RegisteredListener(eid, coro)

        return wrap_func

    async def start_marker_event_listener(self, delay: int = 5) -> None:
        """
        Starts the marker event listener
        :param delay: The delay between marker checking
        :return: None
        """
        self.marker_listener.start(delay)

    def marker_event(self, coro) -> RegisteredListener:
        """
        A Decorator to register an event listener for new map markers

        :param coro: The coroutine to call when the command is called
        :return: RegisteredListener - The listener object
        """

        if isinstance(coro, RegisteredListener):
            coro = coro.get_coro()

        if not self.marker_listener:
            raise ValueError("Marker listener not started")

        listener = RegisteredListener("map_marker", coro)
        self.marker_listener.add_listener(listener)
        return listener

    def protobuf_received(self, coro) -> RegisteredListener:
        """
        A Decorator to register an event listener for protobuf being received on the websocket

        :param coro: The coroutine to call when the command is called
        :return: RegisteredListener - The listener object
        """

        if isinstance(coro, RegisteredListener):
            coro = coro.get_coro()

        listener = RegisteredListener("protobuf_received", coro)
        ProtobufEvent.handlers.register(listener, self.server_id)
        return listener

    def remove_listener(self, listener) -> bool:
        """
        This will remove a listener, command or event. Takes a RegisteredListener instance

        :return: Success of removal. True = Removed. False = Not Removed
        """
        if isinstance(listener, RegisteredListener):
            if listener.listener_id == "map_marker":
                return self.marker_listener.remove_listener(listener)

            if ChatEvent.handlers.has(listener, self.server_id):
                ChatEvent.handlers.unregister(listener, self.server_id)
                return True

            if TeamEvent.handlers.has(listener, self.server_id):
                TeamEvent.handlers.unregister(listener, self.server_id)
                return True

            if EntityEvent.handlers.has(listener, self.server_id):
                EntityEvent.handlers.unregister(listener, self.server_id)
                return True

            if ProtobufEvent.handlers.has(listener, self.server_id):
                ProtobufEvent.handlers.unregister(listener, self.server_id)
                return True

        return False

    @staticmethod
    async def hang() -> None:
        """
        This Will permanently put your script into a state of 'hanging' Cannot be Undone. Only do this in scripts
        using commands

        :returns Nothing, This will never return
        """

        while True:
            await asyncio.sleep(1)

    def get_conversation_factory(self) -> ConversationFactory:
        """
        Gets the current ConversationFactory object

        :returns ConversationFactory: the factory
        """
        return self.remote.conversation_factory

    async def get_time(self) -> RustTime:
        """
        Gets the current in-game time from the server.

        :returns RustTime: The Time
        """
        raise NotImplementedError("Not Implemented")

    async def send_team_message(self, message: str) -> None:
        """
        Sends a message to the in-game team chat

        :param message: The string message to send
        """
        raise NotImplementedError("Not Implemented")

    async def get_info(self) -> RustInfo:
        """
        Gets information on the Rust Server
        :return: RustInfo - The info of the server
        """
        raise NotImplementedError("Not Implemented")

    async def get_team_chat(self) -> List[RustChatMessage]:
        """
        Gets the team chat from the server

        :return List[RustChatMessage]: The chat messages in the team chat
        """
        raise NotImplementedError("Not Implemented")

    async def get_team_info(self) -> RustTeamInfo:
        """
        Gets Information on the members of your team

        :return RustTeamInfo: The info of your team
        """
        raise NotImplementedError("Not Implemented")

    async def get_markers(self) -> List[RustMarker]:
        """
        Gets all the map markers from the server

        :return List[RustMarker]: All the markers on the map
        """
        raise NotImplementedError("Not Implemented")

    async def get_map(
        self,
        add_icons: bool = False,
        add_events: bool = False,
        add_vending_machines: bool = False,
        override_images: dict = None,
        add_grid: bool = False,
    ) -> Image.Image:
        """
        Gets an image of the map from the server with the specified additions

        :param add_icons: To add the monument icons
        :param add_events: To add the Event icons
        :param add_vending_machines: To add the vending icons
        :param override_images: To override the images pre-supplied with RustPlus.py
        :param add_grid: To add the grid to the map
        :return Image: PIL Image
        """
        raise NotImplementedError("Not Implemented")

    async def get_raw_map_data(self) -> RustMap:
        """
        Gets the raw map data from the server

        :return RustMap: The raw map of the server
        """
        raise NotImplementedError("Not Implemented")

    async def get_entity_info(self, eid: int = None) -> RustEntityInfo:
        """
        Gets entity info from the server

        :param eid: The Entities ID
        :return RustEntityInfo: The entity Info
        """
        raise NotImplementedError("Not Implemented")

    async def turn_on_smart_switch(self, eid: int = None) -> None:
        """
        Turns on a given smart switch by entity ID

        :param eid: The Entities ID
        :return None:
        """
        raise NotImplementedError("Not Implemented")

    async def turn_off_smart_switch(self, eid: int = None) -> None:
        """
        Turns off a given smart switch by entity ID

        :param eid: The Entities ID
        :return None:
        """
        raise NotImplementedError("Not Implemented")

    async def promote_to_team_leader(self, steamid: int = None) -> None:
        """
        Promotes a given user to the team leader by their 64-bit Steam ID

        :param steamid: The SteamID of the player to promote
        :return None:
        """
        raise NotImplementedError("Not Implemented")

    @deprecated("Use RustSocket#get_markers")
    async def get_current_events(self) -> List[RustMarker]:
        """
        Returns all the map markers that are for events:
        Can detect:
            - Explosion
            - CH47 (Chinook)
            - Cargo Ship
            - Locked Crate
            - Attack Helicopter

        :return List[RustMarker]: All current events
        """
        raise NotImplementedError("Not Implemented")

    async def get_contents(
        self, eid: int = None, combine_stacks: bool = False
    ) -> RustContents:
        """
        Gets the contents of a storage monitor-attached container

        :param eid: The EntityID Of the storage Monitor
        :param combine_stacks: Whether to combine alike stacks together
        :return RustContents: The contents on the monitor
        """
        raise NotImplementedError("Not Implemented")

    @deprecated("Use RustSocket#get_contents")
    async def get_tc_storage_contents(
        self, eid: int = None, combine_stacks: bool = False
    ) -> RustContents:
        """
        Gets the Information about TC Upkeep and Contents.
        Do not use this for any other storage monitor than a TC
        """
        raise NotImplementedError("Not Implemented")

    async def get_camera_manager(self, id: str) -> CameraManager:
        """
        Gets a camera manager for a given camera ID

        NOTE: This will override the current camera manager if one exists for the given ID so you cannot have multiple

        :param id: The ID of the camera
        :return CameraManager: The camera manager
        :raises RequestError: If the camera is not found or you cannot access it. See reason for more info
        """
        raise NotImplementedError("Not Implemented")
