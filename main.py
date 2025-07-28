from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import subprocess
import uuid
import os
import asyncio
import pty
import select
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

@app.get('/', response_class=HTMLResponse)
async def serve_index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.websocket("/ws/run")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            if data['type'] != 'code':
                continue

            code = data['message']
            if not code.strip():
                await websocket.send_json({'type': 'error', 'message': 'No code provided.'})
                continue

            temp_id = str(uuid.uuid4())[:8]
            code_path = f'temp_{temp_id}.py'

            with open(code_path, 'w', encoding='utf-8') as f:
                f.write(code)

            master_fd = None
            slave_fd = None
            process = None
            try:
                # Use pty to create an interactive session
                master_fd, slave_fd = pty.openpty()
                process = await asyncio.create_subprocess_exec(
                    "python", code_path,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    pass_fds=(slave_fd,)
                )

                async def read_stream():
                    loop = asyncio.get_event_loop()
                    while True:
                        r, _, _ = await loop.run_in_executor(None, lambda: select.select([master_fd], [], [], 0.1))
                        if master_fd in r:
                            data = os.read(master_fd, 1024).decode('utf-8', errors='ignore')
                            if not data:
                                break
                            logger.debug(f"Received: {data.strip()}")
                            await websocket.send_json({'type': 'output', 'message': data})
                            if "input" in data.lower():
                                await handle_input()
                        # Check if process has finished
                        if process.returncode is not None:
                            break

                async def handle_input():
                    await websocket.send_json({'type': 'input', 'message': 'Enter input: '})
                    try:
                        user_data = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
                        if user_data['type'] == 'input':
                            os.write(master_fd, user_data['message'].encode('utf-8'))
                            logger.debug(f"Sent input: {user_data['message']}")
                    except asyncio.TimeoutError:
                        await websocket.send_json({'type': 'error', 'message': 'Input timeout'})

                # Run read_stream
                await read_stream()

                # Ensure process is fully waited on
                return_code = await process.wait()
                logger.debug(f"Process finished with return code: {return_code}")

            except Exception as e:
                await websocket.send_json({'type': 'error', 'message': str(e)})
                logger.error(f"Exception: {str(e)}")
            finally:
                if process:
                    try:
                        if process.returncode is None:
                            process.terminate()
                            await asyncio.wait_for(process.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        process.kill()
                    finally:
                        if master_fd is not None:
                            os.close(master_fd)
                        if slave_fd is not None:
                            os.close(slave_fd)
                if os.path.exists(code_path):
                    os.remove(code_path)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        await websocket.send_json({'type': 'error', 'message': 'WebSocket error: ' + str(e)})
        logger.error(f"WebSocket error: {str(e)}")
        await websocket.close()