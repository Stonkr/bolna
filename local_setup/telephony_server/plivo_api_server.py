import os
import json
import requests
import uuid
from dotenv import load_dotenv
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import plivo
import urllib.parse
import time


app = FastAPI()
load_dotenv()
port = 8002

plivo_auth_id = os.getenv('PLIVO_AUTH_ID')
plivo_auth_token = os.getenv('PLIVO_AUTH_TOKEN')
plivo_phone_number = os.getenv('PLIVO_PHONE_NUMBER')
ngrok_host= os.getenv('NGROK_HOST', 'ngrok')

# Initialize Plivo client
plivo_client = plivo.RestClient(os.getenv('PLIVO_AUTH_ID'), os.getenv('PLIVO_AUTH_TOKEN'))

# Initialize Redis client
redis_pool = redis.ConnectionPool.from_url(os.getenv('REDIS_URL'), decode_responses=True)
redis_client = redis.Redis.from_pool(redis_pool)

def populate_ngrok_tunnels():
    response = requests.get(f"http://{ngrok_host}:4040/api/tunnels")  # ngrok interface
    telephony_url, bolna_url = None, None

    if response.status_code == 200:
        data = response.json()

        for tunnel in data['tunnels']:
            if tunnel['name'] == 'plivo-app':
                telephony_url = tunnel['public_url']
            elif tunnel['name'] == 'bolna-app':
                bolna_url = tunnel['public_url'].replace('https:', 'wss:')

        return telephony_url, bolna_url
    else:
        print(f"Error: Unable to fetch data. Status code: {response.status_code}")

@app.post('/call')
async def make_call(request: Request):
    try:
        call_details = await request.json()
        agent_id = call_details.get('agent_id', None)
        context_data = call_details.get('context_data', None)  # Read context_data

        if not agent_id:
            raise HTTPException(status_code=404, detail="Agent not provided")

        if not call_details or "recipient_phone_number" not in call_details:
            raise HTTPException(status_code=404, detail="Recipient phone number not provided")

        telephony_host, bolna_host = populate_ngrok_tunnels()

        print(f'telephony_host: {telephony_host}')
        print(f'bolna_host: {bolna_host}')

        # Generate call_id
        timestamp = int(time.time())
        call_id = f"{agent_id}#{timestamp}"
        
        # Store context_data in Redis
        if context_data:
            if 'recipient_data' not in context_data:
                raise HTTPException(status_code=400, detail="recipient_data not provided in context_data")
            await redis_client.set(f"{call_id}_context_data", json.dumps(context_data))


        # adding hangup_url since plivo opens a 2nd websocket once the call is cut.
        # https://github.com/bolna-ai/bolna/issues/148#issuecomment-2127980509
        encoded_call_id = urllib.parse.quote(call_id)
        call = plivo_client.calls.create(
            from_=plivo_phone_number,
            to_=call_details.get('recipient_phone_number'),
            answer_url=f"{telephony_host}/plivo_connect?bolna_host={bolna_host}&agent_id={agent_id}&to_phone_number={call_details.get('recipient_phone_number')}&call_id={encoded_call_id}",
            hangup_url=f"{telephony_host}/plivo_hangup_callback",
            answer_method='POST')

        return JSONResponse({"call_id": call_id}, status_code=200)

    except HTTPException as e:
        print(f"Exception occurred in make_call: {e}")
        raise e  # Re-raise the HTTPException to send the same status code and message
    except Exception as e:
        print(f"Exception occurred in make_call: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.post('/plivo_connect')
async def plivo_connect(request: Request, bolna_host: str = Query(...), agent_id: str = Query(...), to_phone_number: str = Query(...), call_id: str = Query(...)):
    try:
        encoded_phone_number = urllib.parse.quote(to_phone_number)
        encoded_call_id = urllib.parse.quote(call_id)
        
        bolna_websocket_url = f'{bolna_host}/chat/v1/{agent_id}?call_id={encoded_call_id}&amp;to_phone_number={encoded_phone_number}'

        response = f'''
        <Response>
            <Record fileFormat="mp3" maxLength="240" recordSession="true"/>
            <Stream bidirectional="true" keepCallAlive="true">{bolna_websocket_url}</Stream>
        </Response>
        '''
        print(response)

        return PlainTextResponse(str(response), status_code=200, media_type='text/xml')

    except Exception as e:
        print(f"Exception occurred in plivo_connect: {e}")

@app.post('/plivo_hangup_callback')
async def plivo_hangup_callback(request: Request):
    # add any post call hangup processing
    return PlainTextResponse("", status_code=200)
