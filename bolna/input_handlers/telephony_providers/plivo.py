from bolna.input_handlers.telephony import TelephonyInputHandler
from dotenv import load_dotenv
from bolna.helpers.logger_config import configure_logger
import aiohttp
logger = configure_logger(__name__)
import asyncio
import os
load_dotenv()
import base64

class PlivoInputHandler(TelephonyInputHandler):
    def __init__(self, queues, websocket=None, input_types=None, mark_set=None, turn_based_conversation=False):
        super().__init__(queues, websocket, input_types, mark_set, turn_based_conversation)
        self.io_provider = 'plivo'

    async def call_start(self, packet):
        start = packet['start']
        self.call_sid = start['callId']
        self.stream_sid = start['streamId']

    async def stop_handler(self):
        await asyncio.sleep(2)
        try:
            # Inform Plivo to end the call
            async with aiohttp.ClientSession() as session:
                auth_id = os.getenv('PLIVO_AUTH_ID')
                auth_token = os.getenv('PLIVO_AUTH_TOKEN')
                
                # Create the Basic Auth token
                auth_string = f"{auth_id}:{auth_token}"
                auth_bytes = auth_string.encode('ascii')
                base64_bytes = base64.b64encode(auth_bytes)
                base64_auth = base64_bytes.decode('ascii')
                plivo_url = f"https://api.plivo.com/v1/Account/{os.getenv('PLIVO_AUTH_ID')}/Call/{self.call_sid}/Stream/{self.stream_sid}/"
                headers = {
                    'Authorization': f'Basic {base64_auth}',
                    'Content-Type': 'application/json'
                }
                async with session.delete(plivo_url, headers=headers) as response:
                    if response.status == 204:
                        logger.info("Successfully informed Plivo to end the call.")
                    else:
                        logger.error(f"Failed to end the call with Plivo. Status: {response.status}")
        except Exception as e:
            logger.error(f"Exception while trying to end the call with Plivo: {e}")
        finally:
            # Perform any additional cleanup if necessary
            await super().stop_handler()
