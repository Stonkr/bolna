import csv
import aiofiles
import os
import asyncio
import uuid
import traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import redis.asyncio as redis
from dotenv import load_dotenv
from bolna.helpers.utils import store_file, get_md5_hash
from bolna.prompts import *
from bolna.helpers.logger_config import configure_logger
from bolna.models import *
from bolna.llms import LiteLLM
from bolna.agent_manager.assistant_manager import AssistantManager
from bolna.helpers.utils import get_s3_file
from fastapi.responses import JSONResponse
import aiohttp
import urllib
from bolna.providers import SUPPORTED_SYNTHESIZER_MODELS

load_dotenv()
logger = configure_logger(__name__)
BUCKET_NAME=os.getenv('BUCKET_NAME')

redis_pool = redis.ConnectionPool.from_url(os.getenv('REDIS_URL'), decode_responses=True)
redis_client = redis.Redis.from_pool(redis_pool)
logger.info("Redis connection pool initialized")

active_websockets: List[WebSocket] = []
# Initialize an async lock for thread safety
file_lock = asyncio.Lock()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


class CreateAgentPayload(BaseModel):
    agent_config: AgentModel
    agent_prompts: Optional[Dict[str, Dict[str, str]]]

def generate_agent_uuid() -> str:
    agent_uuid = str(uuid.uuid4())
    agent_uuid = f"{get_environment_prefix()}{agent_uuid}"
    return agent_uuid

def get_environment_prefix() -> str:
    environment = os.getenv('ENVIRONMENT', 'production')
    return f"{environment}_" if environment != 'production' else ''


@app.post("/agent")
async def create_agent(agent_data: CreateAgentPayload):
    agent_uuid = generate_agent_uuid()
    data_for_db = agent_data.agent_config.model_dump()
    data_for_db["assistant_status"] = "seeding"
    agent_prompts = agent_data.agent_prompts
    print("agent_prompts", agent_prompts)
    logger.info(f'Data for DB {data_for_db}')

    if len(data_for_db['tasks']) > 0:
        logger.info("Setting up follow up tasks")
        for index, task in enumerate(data_for_db['tasks']):
            if task['task_type'] == "extraction":
                extraction_prompt_llm = os.getenv("EXTRACTION_PROMPT_GENERATION_MODEL")
                extraction_prompt_generation_llm = LiteLLM(model=extraction_prompt_llm, max_tokens=2000)
                extraction_prompt = await extraction_prompt_generation_llm.generate(
                    messages=[
                        {'role': 'system', 'content': EXTRACTION_PROMPT_GENERATION_PROMPT},
                        {'role': 'user', 'content': data_for_db["tasks"][index]['tools_config']["llm_agent"]['extraction_details']}
                    ])
                data_for_db["tasks"][index]["tools_config"]["llm_agent"]['extraction_json'] = extraction_prompt

    stored_prompt_file_path = f"{agent_uuid}/conversation_details.json"
    await asyncio.gather(
        redis_client.set(agent_uuid, json.dumps(data_for_db)),
        store_file(bucket_name=BUCKET_NAME, file_key=stored_prompt_file_path, file_data=agent_prompts, local=False)
    )

    # Create and store welcome message audio
    welcome_message = data_for_db.get("agent_welcome_message", "")
    if welcome_message:
        synthesizer_config = data_for_db['tasks'][0]['tools_config']['synthesizer']
        synthesizer_provider = synthesizer_config['provider']
        synthesizer_class = SUPPORTED_SYNTHESIZER_MODELS.get(synthesizer_provider)
        if synthesizer_class:
            synthesizer = synthesizer_class(**synthesizer_config['provider_config'])
            audio_data = await synthesizer.synthesize(welcome_message)
            audio_file_name = f"{get_md5_hash(welcome_message)}.wav"
            await store_file(bucket_name=BUCKET_NAME, file_key=f"{agent_uuid}/audio/{audio_file_name}", file_data=audio_data, content_type="wav", local=False)

    return {"agent_id": agent_uuid, "state": "created"}


############################################################################################# 
# Websocket 
#############################################################################################
@app.websocket("/chat/v1/{agent_id}")
async def websocket_endpoint(agent_id: str, websocket: WebSocket, user_agent: str = Query(None), to_phone_number: str = Query(None), call_id: str = Query(None)):
    logger.info("Connected to ws")
    await websocket.accept()
    active_websockets.append(websocket)
    agent_config, context_data = None, None
    try:
        retrieved_agent_config = await redis_client.get(agent_id)
        logger.info(
            f"Retrieved agent config: {retrieved_agent_config}")
        agent_config = json.loads(retrieved_agent_config)
        # Retrieve context_data from Redis
        context_data_json = await redis_client.get(f"{call_id}_context_data")
        if context_data_json:
            logger.info(
                f"Retrieved context data: {retrieved_agent_config}")
            context_data = json.loads(context_data_json)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=404, detail="Agent not found")

    assistant_manager = AssistantManager(agent_config, websocket, agent_id, context_data)

    try:
        async for index, task_output in assistant_manager.run(local=False, run_id=call_id):
            logger.info(task_output)
            await append_to_csv(task_output, to_phone_number) # Passing to_phone_number to the function
            await send_to_webhook(task_output, to_phone_number) # Send details to webhook
    except WebSocketDisconnect:
        active_websockets.remove(websocket)
    except Exception as e:
        traceback.print_exc()
        logger.error(f"error in executing {e}")

@app.get("/agent/{agent_id}")
async def get_agent_details(agent_id: str):
    try:
        agent_data = await redis_client.get(agent_id)
        if agent_data:
            return json.loads(agent_data)
        else:
            raise HTTPException(status_code=404, detail="Agent not found")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/agents-keys")
async def list_agents():
    environment_prefix = get_environment_prefix()
    agent_keys = await redis_client.keys(f'{environment_prefix}*')
    return {"agents_keys": agent_keys}

@app.get("/agents")
async def list_agents():
    environment_prefix = get_environment_prefix()
    agent_keys = await redis_client.keys(f'{environment_prefix}*')
    
    if agent_keys:
        agent_values = await redis_client.mget(agent_keys)
        agents = {key: json.loads(value) for key, value in zip(agent_keys, agent_values) if value}
    else:
        agents = {}

    return {"agents": agents}

@app.get("/agent/{agent_id}/conversation_details")
async def get_conversation_details(agent_id: str):
    try:
        stored_prompt_file_path = f"{agent_id}/conversation_details.json"
        
        # Fetch the file from S3 using the utility function
        file_content = await get_s3_file(bucket_name=BUCKET_NAME, file_key=stored_prompt_file_path)
        if file_content is None:
            raise HTTPException(status_code=404, detail="Conversation details not found")
        
        conversation_details = json.loads(file_content.decode('utf-8'))
        
        return JSONResponse(content=conversation_details, status_code=200)
    except HTTPException as e:
        raise e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal Server Error")


async def send_to_webhook(task_output, to_phone_number):
    run_id=task_output.get('run_id', '')
    encoded_call_id = urllib.parse.quote(run_id)
    webhook_url = os.path.join(os.getenv('UPDATE_CALL_STATUS_WEBHOOK_URL'), encoded_call_id)
    if webhook_url:
        payload = {
            "to_number": to_phone_number,
            "call_sid": task_output.get('call_sid', ''),
            "stream_sid": task_output.get('stream_sid', ''),
            "conversation_time": task_output.get('conversation_time', ''),
            "ended_by_assistant": task_output.get('ended_by_assistant', ''),
            "transcript": "\n".join([f"{msg['role']}: {msg['content']}" for msg in task_output.get('messages', []) if msg['role'] != 'system'])
        }
        headers = {'Content-Type': 'application/json'}
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, headers=headers, json=payload) as response:
                if response.status == 200:
                    logger.info(f"Successfully sent data to webhook: {response.status}")
                else:
                    logger.error(f"Error: {response.status} - {await response.text()}")


async def append_to_csv(task_output, to_phone_number): # Added to_phone_number parameter
    # Extract required values
    run_id = task_output.get('run_id', '')
    call_sid = task_output.get('call_sid', '')
    stream_sid = task_output.get('stream_sid', '')
    conversation_time = task_output.get('conversation_time', '')
    ended_by_assistant = task_output.get('ended_by_assistant', '')

    # Format the transcript
    messages = task_output.get('messages', [])
    transcript = "\n".join([f"{msg['role']}: {msg['content']}" for msg in messages if msg['role'] != 'system'])

    # Define the file path
    file_path = 'agent_data/calls.csv'

    # Ensure the directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # Write or append to the CSV file
    async with file_lock:
        file_exists = os.path.isfile(file_path)
        async with aiofiles.open(file_path, mode='a', newline='') as file:
            if not file_exists:
                # Write the header if the file doesn't exist
                await file.write('run_id,to_number,call_sid,stream_sid,conversation_time,ended_by_assistant,transcript\n')
            print(transcript)
            # Write the data row
            writer = csv.writer(file)
            await writer.writerow([run_id, to_phone_number, call_sid, stream_sid, conversation_time, ended_by_assistant, transcript])

@app.get("/healthcheck")
async def healthcheck():
    return {"status": "Version 0.0.1"}

import aiofiles
import asyncio
from csv import DictReader
from io import StringIO

@app.get("/calls/{agent_id}")
async def get_calls_by_agent(agent_id: str):
    file_path = 'agent_data/calls.csv'
    calls = []
    
    async def process_chunk(chunk):
        reader = DictReader(StringIO(chunk))
        return [
            {key: value.strip() for key, value in row.items()} 
            for row in reader if row['run_id'].startswith(agent_id)
        ]
    
    async with aiofiles.open(file_path, mode='r') as file:
        chunk_size = 1024 * 1024  # 1MB chunks
        chunks = []
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
        
        results = await asyncio.gather(*[process_chunk(chunk) for chunk in chunks])
        calls = [call for sublist in results for call in sublist]
    
    return {"calls": calls}

@app.get("/calls/{agent_id}/{to_number}")
async def get_calls_by_agent_and_number(agent_id: str, to_number: str):
    file_path = 'agent_data/calls.csv'
    calls = []
    to_number = to_number.lstrip('+')
    
    async def process_chunk(chunk):
        reader = DictReader(StringIO(chunk))
        return [
            {key: value.strip() for key, value in row.items()} 
            for row in reader if row['run_id'].startswith(agent_id) and row['to_number'].lstrip('+').strip() == to_number
        ]
        
    async with aiofiles.open(file_path, mode='r') as file:
        chunk_size = 1024 * 1024  # 1MB chunks
        chunks = []
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
        
        results = await asyncio.gather(*[process_chunk(chunk) for chunk in chunks])
        calls = [call for sublist in results for call in sublist]
    
    return {"calls": calls}


from bolna.helpers.utils import get_raw_audio_bytes

@app.get("/get-audio")
async def get_audio_bytes():
    """
    API endpoint to get raw audio bytes.
    """
    file_name= "agent_data/Jenny/welcome_audios/af102a8571e8a1aa296eb806ff14f96c.wav"
    try:
        audio_data = await get_raw_audio_bytes(filename = file_name, local=True, is_location=True)
        if audio_data is None:
            raise HTTPException(status_code=404, detail="Audio file not found")
        
        return JSONResponse(content={"audio_data": audio_data.hex()}, status_code=200)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal Server Error")
