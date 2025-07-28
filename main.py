from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import subprocess
import uuid
import os
import asyncio
import pty
import select
import logging
import re

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
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
                master_fd, slave_fd = pty.openpty()

                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["TERM"] = "xterm"
                # Ensure locale is set to UTF-8 for consistent string handling
                env["LANG"] = "C.UTF-8"
                env["LC_ALL"] = "C.UTF-8"

                # Use python -u for unbuffered streams
                process = await asyncio.create_subprocess_exec(
                    "python", "-u", code_path,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    pass_fds=(slave_fd,),
                    env=env
                )

                input_prompt_event = asyncio.Event()
                read_buffer = "" # Buffer for accumulating output chunks

                async def read_pty_output():
                    nonlocal read_buffer
                    loop = asyncio.get_event_loop()
                    while True:
                        try:
                            r, _, _ = await loop.run_in_executor(None, lambda: select.select([master_fd], [], [], 0.001))
                            if master_fd in r:
                                # Read as many bytes as possible (up to 4KB)
                                chunk = os.read(master_fd, 4096).decode('utf-8', errors='ignore')
                                if not chunk: # EOF, stream closed
                                    break

                                logger.debug(f"Raw PTY Read: {repr(chunk)}")
                                read_buffer += chunk

                                # Heuristic for input prompt detection:
                                # A common characteristic of Python's input() is that it sends its prompt
                                # and *does not* end with a newline, then blocks for user input (ending in newline).
                                if process.returncode is None and \
                                   read_buffer and \
                                   not read_buffer.endswith(('\n', '\r')) and \
                                   re.search(r'(?i)\b(?:enter|input|value|name|number|prompt|string|text)\b', read_buffer[-min(len(read_buffer), 100):]): # Check last 100 chars for keywords
                                    
                                    logger.debug(f"Detected potential input prompt: {repr(read_buffer)}")
                                    await websocket.send_json({'type': 'output', 'message': read_buffer})
                                    read_buffer = "" # Clear buffer after sending prompt to client
                                    
                                    # Signal that input is needed and wait for it to be handled
                                    await handle_input(input_prompt_event)
                                    input_prompt_event.clear() # Reset event for next input

                                else:
                                    # Process and send complete lines
                                    while '\n' in read_buffer or '\r' in read_buffer:
                                        newline_pos = -1
                                        if '\n' in read_buffer:
                                            newline_pos = read_buffer.find('\n')
                                        if '\r' in read_buffer:
                                            if newline_pos == -1 or read_buffer.find('\r') < newline_pos:
                                                newline_pos = read_buffer.find('\r')

                                        if newline_pos != -1:
                                            line_to_send = read_buffer[:newline_pos + 1]
                                            read_buffer = read_buffer[newline_pos + 1:]
                                            # Normalize newlines for consistent display on frontend
                                            await websocket.send_json({'type': 'output', 'message': line_to_send.replace('\r\n', '\n').replace('\r', '\n')})
                                        else:
                                            break # No more full lines

                                # If there's remaining data in the buffer after processing lines,
                                # it means it's a partial line that doesn't end with a newline.
                                # This partial line could be a non-prompt output or the start of a multi-chunk prompt.
                                # We keep it in the buffer for the next read cycle.
                                if read_buffer and '\n' not in read_buffer and '\r' not in read_buffer and process.returncode is None:
                                     # If it's a partial line and process is still running,
                                     # send it now as partial output to ensure responsiveness for long lines
                                     # or if the prompt comes in multiple chunks.
                                     await websocket.send_json({'type': 'output', 'message': read_buffer})
                                     read_buffer = "" # Send and clear, rely on the next read for new data

                            elif process.returncode is not None:
                                # Process has finished, read any remaining buffered data then exit
                                break # Exit loop, final read will happen below
                            
                        except BlockingIOError:
                            pass
                        except Exception as e:
                            logger.exception("Error during PTY read loop.")
                            await websocket.send_json({'type': 'error', 'message': f"Internal PTY read error: {e}"})
                            break
                        
                        await asyncio.sleep(0.001)

                async def handle_input(event: asyncio.Event):
                    await websocket.send_json({'type': 'input', 'message': ''})
                    try:
                        user_data = await asyncio.wait_for(websocket.receive_json(), timeout=120.0)
                        if user_data['type'] == 'input':
                            # Ensure input is encoded correctly and ends with a newline
                            input_value = user_data['message']
                            os.write(master_fd, input_value.encode('utf-8'))
                            logger.debug(f"Sent input to PTY: {repr(input_value)}")
                        else:
                            await websocket.send_json({'type': 'error', 'message': 'Unexpected message type for input.'})
                    except asyncio.TimeoutError:
                        await websocket.send_json({'type': 'error', 'message': 'Input timed out (120s). Program might be stuck.'})
                        logger.warning("Input timeout.")
                        # Attempt to send a newline to unblock in some cases
                        os.write(master_fd, b'\n')
                    except Exception as input_e:
                        await websocket.send_json({'type': 'error', 'message': f'Error handling client input: {input_e}'})
                        logger.exception("Error handling client input.")
                    finally:
                        event.set() # Release the read_pty_output from waiting

                reader_task = asyncio.create_task(read_pty_output())

                await process.wait()

                try:
                    await asyncio.wait_for(reader_task, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Reader task did not finish in time, cancelling.")
                    reader_task.cancel()
                    await asyncio.sleep(0.1)

                # Read any final remaining data from the PTY after process exit
                final_pty_output = ""
                try:
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0)
                        if master_fd in r:
                            chunk = os.read(master_fd, 4096).decode('utf-8', errors='ignore')
                            if not chunk:
                                break
                            final_pty_output += chunk
                        else:
                            break
                except BlockingIOError:
                    pass

                if final_pty_output:
                    # Normalize newlines for final output
                    await websocket.send_json({'type': 'output', 'message': final_pty_output.replace('\r\n', '\n').replace('\r', '\n')})
                
                logger.debug(f"Process finished with return code: {process.returncode}")
                if process.returncode == 0:
                    await websocket.send_json({'type': 'output', 'message': '\nProgram finished successfully.'})
                else:
                    await websocket.send_json({'type': 'output', 'message': f'\nProgram exited with code {process.returncode}.'})

            except Exception as e:
                await websocket.send_json({'type': 'error', 'message': f'Server execution error: {str(e)}'})
                logger.exception("Error during execution setup or process management.")
            finally:
                if process and process.returncode is None:
                    logger.warning("Process still running in finally block, terminating.")
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.error("Process termination timed out, killing.")
                        process.kill()
                if master_fd is not None:
                    os.close(master_fd)
                if slave_fd is not None:
                    os.close(slave_fd)
                if os.path.exists(code_path):
                    os.remove(code_path)
                if input_prompt_event.is_set():
                    input_prompt_event.clear()

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected gracefully.")
    except Exception as e:
        await websocket.send_json({'type': 'error', 'message': 'WebSocket communication error: ' + str(e)})
        logger.exception("Unhandled WebSocket error.")
        await websocket.close()