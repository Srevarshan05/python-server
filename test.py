import asyncio
import websockets
import json

# Replace with your actual Space URL
# For Hugging Face Spaces, use wss://<your-space-id>.hf.space/ws
WS_URL = "wss://titan1502-Python-server.hf.space/ws"

async def run_python_code(code_to_execute: str):
    try:
        async with websockets.connect(WS_URL) as websocket:
            print("Connected to WebSocket.")

            # Send the Python code
            await websocket.send(json.dumps({"type": "code", "content": code_to_execute}))
            print(f"Sent code:\n---\n{code_to_execute}\n---")

            full_output = ""
            execution_complete = False

            while not execution_complete:
                try:
                    # Keep waiting for messages until execution_complete
                    # Increased timeout to 60 seconds as per your last script
                    message = await asyncio.wait_for(websocket.recv(), timeout=60)
                    data = json.loads(message)

                    if data["type"] == "output":
                        full_output += data["content"]
                        print(f"Received output chunk: {data['content'].strip()}")
                    elif data["type"] == "input_request":
                        print("\n--- Input Requested ---")
                        user_input = input("Please enter input for the script: ")
                        # IMPORTANT: Add newline character for the server's readline() to process it as a complete line
                        await websocket.send(json.dumps({"type": "input", "content": user_input + "\n"}))
                        print(f"Sent input: {user_input}")
                    elif data["type"] == "execution_complete":
                        print("\n--- Execution Complete ---")
                        # The 'result' field from the server's execution_complete message
                        server_result = data['result']
                        print(f"Result details from server: {server_result}")

                        # Consolidate output from the server_result as well
                        if server_result.get('output'):
                            full_output += server_result['output']
                        if server_result.get('error'):
                            # Prepend "Error:" if it's the first error message
                            if "Error from server:" not in full_output:
                                full_output += "\nError from server:\n"
                            full_output += server_result['error']
                            
                        execution_complete = True
                    elif data["type"] == "pong":
                        # print("Received pong.") # Optional: uncomment to see pongs
                        pass
                    else:
                        print(f"Received unknown message type: {data['type']} - {data.get('content')}")

                except asyncio.TimeoutError:
                    print("Timeout: No message received from server for 60 seconds. Disconnecting.")
                    break
                except websockets.exceptions.ConnectionClosedOK:
                    print("Connection closed by server (OK).")
                    # If it closes OK before execution_complete, it's an issue
                    if not execution_complete:
                        print("WARNING: Connection closed OK before execution_complete message was received!")
                    break
                except websockets.exceptions.ConnectionClosedError as e:
                    print(f"Connection closed with error: {e}")
                    break
                except json.JSONDecodeError:
                    print(f"Received invalid JSON: {message}")
                    break
                except Exception as e:
                    print(f"An unexpected error occurred during message reception: {e}")
                    break

            print("\n--- Final Consolidated Output ---")
            print(full_output)
            return full_output

    except websockets.exceptions.InvalidURI as e:
        print(f"Invalid WebSocket URI: {e}")
    except ConnectionRefusedError:
        print("Connection refused. Is the server running and accessible?")
    except Exception as e:
        print(f"Could not connect to WebSocket: {e}")
    return None

if __name__ == "__main__":
    # Test 1: Simple print statement (Matches the current app.py test 1)
    print("\n=== Running Test 1: Simple Print ===")
    asyncio.run(run_python_code(code_to_execute = "print('Hello, Space!')"))

    # Test 2: Code with interactive input (corrected syntax)
    print("\n=== Running Test 2: Interactive Input ===")
    interactive_code = """
name = input("Enter your name: ")
age = int(input("How old are you? "))
print(f"Hello, {name}! You are {age} years old.")
"""
    # For app.py's Test 2 (where exec is not yet active)
    # asyncio.run(run_python_code(code_to_execute="DUMMY_CODE_FOR_TESTING_SYS_REDIRECTION"))

    # Once app.py has Test 3 (with exec) uncomment this:
    asyncio.run(run_python_code(interactive_code))

    # Test 3: Code with error (uncomment when full exec is working)
    # print("\n=== Running Test 3: Code with Error ===")
    # asyncio.run(run_python_code("print(1/0)"))

    # Test 4: Long-running code (will hit timeout if not finished, uncomment when full exec is working)
    # print("\n=== Running Test 4: Long Running Code ===")
    # asyncio.run(run_python_code("import time\nfor i in range(5):\n    print(f'Counting: {i}')\n    time.sleep(1)"))