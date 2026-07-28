import asyncio
import json
import logging
import threading
from typing import Callable, List, Union

import websockets
from websockets import WebSocketException

from ..Config import APIConfig
from ..models.Enums import EventType
from ..models.WebsocketEvent import WebsocketEvent

logger = logging.getLogger("iDrive")


class WebsocketManager:
    def __init__(self):
        self._ws_thread = None
        self._stop_ws = threading.Event()
        self._connected = threading.Event()

        self._callbacks: List[Callable[[WebsocketEvent], None]] = []
        self._loop: Union[asyncio.AbstractEventLoop, None] = None
        self._ws = None

        self._forced_logout = False

    def start_websocket(self):
        if self.is_running():
            return

        self._stop_ws.clear()
        self._forced_logout = False

        self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True)
        self._ws_thread.start()

    def stop_websocket(self):
        self._stop_ws.set()

        if self._ws and self._loop:
            async def _close():
                try:
                    await self._ws.close()
                except Exception:
                    pass

            asyncio.run_coroutine_threadsafe(_close(), self._loop)

    def wait_until_connected(self, timeout: float = 5.0) -> bool:
        if not self.is_running():
            raise RuntimeError("WebSocket thread is not running.")
        return self._connected.wait(timeout=timeout)

    def run_forever(self):
        if self._ws_thread is None:
            raise RuntimeError("WebSocket thread not started. Call start_websocket() first.")

        self._ws_thread.join()

    def is_running(self) -> bool:
        return self._ws_thread is not None and self._ws_thread.is_alive()

    def register_callback(self, callback: Callable[[WebsocketEvent], None]):
        self._callbacks.append(callback)

    def send_json(self, message: dict):
        return self.send_message(json.dumps(message))

    def send_message(self, message: str):
        if not self._connected.is_set() or not self._ws:
            raise RuntimeError("WebSocket is not connected yet.")

        async def _send():
            await self._ws.send(message)

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    def _run_ws_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._listen_websocket())
        finally:
            # Close remaining tasks gracefully
            pending = asyncio.all_tasks(loop=self._loop)
            for task in pending:
                task.cancel()

            try:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            except Exception:
                pass

            self._loop.close()
            logger.info("Websocket event loop closed cleanly.")

    async def _listen_websocket(self):
        while not self._stop_ws.is_set():
            try:
                async with websockets.connect(
                    APIConfig.base_ws + '/user',
                    additional_headers={"Authorization": f"Bearer {APIConfig.token}"}
                ) as ws:

                    self._ws = ws
                    self._connected.set()
                    logger.info("Websocket connection established!")

                    async for raw in ws:
                        if self._stop_ws.is_set():
                            break
                        self._handle_ws_event(raw)

            except WebSocketException as e:
                if self._stop_ws.is_set():
                    break
                logger.warning(f"⚠️ WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

            finally:
                self._ws = None
                self._connected.clear()

                if self._forced_logout:
                    logger.info("Stopping websocket due to forced logout.")
                    break

        logger.info("Websocket listener stopped cleanly.")

    def _handle_ws_event(self, data: str):
        if data == "PING":
            # logger.debug(f"Received PING, sending PONG!")
            self.send_message("PONG")
            return
        try:
            event = WebsocketEvent(json.loads(data))
            logger.info(f"Received WebSocket event: {event}")

            # Check forced logout first
            if not event.is_encrypted and event.type == EventType.FORCE_LOGOUT:
                logger.warning("⚠️ Forced logout — shutting down gracefully.")
                self._forced_logout = True
                self.stop_websocket()
                return  # DO NOT raise inside the listener thread

            # Dispatch event to user callbacks
            self._dispatch_event(event)

        except Exception as e:
            logger.exception(f"Error during handling of websocket event: {e}")

    def _dispatch_event(self, event: WebsocketEvent):
        for cb in self._callbacks:
            threading.Thread(
                target=lambda: cb(event),
                daemon=True
            ).start()
