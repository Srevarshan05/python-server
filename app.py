import sys
import io
import traceback
import ast
import time
import json
import os
from contextlib import redirect_stdout, redirect_stderr
from typing import Dict, Any, Optional
import threading
import asyncio
import signal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel
from starlette.responses import HTMLResponse

# Set up proper signal handling for Docker
def signal_handler(signum, frame):
    print(f"Received signal {signum}, shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# --- Custom IO Redirection for Interactive Execution ---

class WebSocketInputOutput:
    """
    Redirects stdin/stdout/stderr to/from a WebSocket connection.
    Handles interactive input by waiting for messages from the client.
    """
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.input_queue = asyncio.Queue()
        self.output_buffer = io.StringIO()
        self.error_buffer = io.StringIO()
        self.closed = False

    async def write(self, s: str):
        """Writes to stdout/stderr and sends to WebSocket."""
        self.output_buffer.write(s)
        try:
            # Send output in chunks or lines to prevent large messages
            await self.websocket.send_text(json.dumps({"type": "output", "content": s}))
        except WebSocketDisconnect:
            self.closed = True
            print("WebSocket disconnected during write.")
        except Exception as e:
            print(f"Error sending output over WebSocket: {e}")
            self.closed = True

    def flush(self):
        """Flushes the buffer (no-op for now, as we send immediately)."""
        pass

    async def readline(self) -> str:
        """Reads a line from stdin, waiting for input from WebSocket."""
        try:
            await self.websocket.send_text(json.dumps({"type": "input_request"}))
            line = await self.input_queue.get() # Wait for input from client
            return line
        except WebSocketDisconnect:
            self.closed = True
            print("WebSocket disconnected during readline.")
            raise EOFError("Input stream closed due to WebSocket disconnect")
        except Exception as e:
            print(f"Error requesting input over WebSocket: {e}")
            self.closed = True
            raise EOFError(f"Input stream error: {e}")

    # For compatibility with sys.stdin, which expects a file-like object
    def _get_input_line(self):
        """Synchronous wrapper for readline for use with exec."""
        # This is a bit tricky: exec is synchronous, but WebSocket is async.
        # We need to bridge this. A simple way is to run the async part
        # in the event loop, but it's usually better to refactor the executor
        # to be fully async if input() is truly interactive.
        # For this example, we'll use a blocking call to asyncio.run
        # which is generally discouraged in a running event loop,
        # but works for simple cases or if the exec is in a separate thread.
        # A more robust solution involves a custom Future/Event loop integration.
        try:
            # This will block the current thread until input is available
            return asyncio.run(self.readline())
        except Exception as e:
            print(f"Synchronous input wrapper error: {e}")
            raise

    def read(self, n=-1):
        """Reads n characters. For simplicity, we'll treat it as readline."""
        return self._get_input_line() # Or implement more complex buffering

    def __getattr__(self, name):
        """Delegate other attributes if needed, or raise AttributeError."""
        # This is a placeholder; real implementation would be more robust
        # For now, we only need write, flush, and readline.
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


# --- Python Syntax Validation FSM (unchanged) ---

class PythonSyntaxFSM:
    """Finite State Machine for Python syntax validation"""
    
    def __init__(self):
        self.reset()
        
    def reset(self):
        self.state = 'normal'
        self.bracket_stack = []
        self.in_string = False
        self.string_delimiter = None
        self.line_number = 0
        self.errors = []
        self.warnings = []
        
    def validate_code(self, code: str) -> Dict[str, Any]:
        """Validate Python code using FSM approach"""
        self.reset()
        
        if not code or not code.strip():
            return {
                'valid': False,
                'errors': [{'line': 0, 'message': 'Empty code input', 'type': 'input_error'}],
                'warnings': [],
                'total_issues': 1
            }
        
        lines = code.split('\n')
        
        for i, line in enumerate(lines):
            self.line_number = i + 1
            self.validate_line(line)
            
        # Check for unclosed brackets at end
        if self.bracket_stack:
            self.errors.append({
                'line': self.line_number,
                'type': 'syntax_error',
                'message': f'Unclosed bracket: {self.bracket_stack[-1]}',
                'severity': 'error'
            })
            
        return {
            'valid': len(self.errors) == 0,
            'errors': self.errors,
            'warnings': self.warnings,
            'total_issues': len(self.errors) + len(self.warnings)
        }
    
    def validate_line(self, line: str):
        """Validate a single line using FSM logic"""
        if not line.strip():
            return
            
        # Check indentation
        leading_spaces = len(line) - len(line.lstrip())
        if leading_spaces % 4 != 0 and line.strip():
            self.warnings.append({
                'line': self.line_number,
                'type': 'style_warning',
                'message': 'Inconsistent indentation (PEP 8 recommends 4 spaces)',
                'severity': 'warning'
            })
        
        # Process character by character for brackets and strings
        i = 0
        while i < len(line):
            char = line[i]
            
            # Handle string states
            if self.in_string:
                if char == self.string_delimiter and (i == 0 or line[i-1] != '\\'):
                    self.in_string = False
                    self.string_delimiter = None
                i += 1
                continue
                
            # Handle comment detection
            if char == '#':
                break   # Rest of line is comment
                
            # Handle string detection
            if char in ['"', "'"]:
                # Check for triple quotes
                if i + 2 < len(line) and line[i:i+3] == char * 3:
                    self.string_delimiter = char * 3
                    i += 3
                else:
                    self.string_delimiter = char
                    i += 1
                self.in_string = True
                continue
                
            # Handle brackets
            if char in '([{':
                self.bracket_stack.append(char)
            elif char in ')]}':
                if not self.bracket_stack:
                    self.errors.append({
                        'line': self.line_number,
                        'type': 'syntax_error',
                        'message': f'Unmatched closing bracket: {char}',
                        'severity': 'error'
                    })
                else:
                    expected = {'(': ')', '[': ']', '{': '}'}
                    last_open = self.bracket_stack[-1]
                    if expected[last_open] != char:
                        self.errors.append({
                            'line': self.line_number,
                            'type': 'syntax_error',
                            'message': f'Mismatched brackets: expected {expected[last_open]}, got {char}',
                            'severity': 'error'
                        })
                    else:
                        self.bracket_stack.pop()
                        
            i += 1
        
        # Check Python syntax patterns
        stripped = line.strip()
        control_keywords = ['def ', 'class ', 'if ', 'elif ', 'for ', 'while ', 'try:', 'except']
        
        for keyword in control_keywords:
            if stripped.startswith(keyword):
                if not stripped.endswith(':') and keyword != 'except':
                    self.errors.append({
                        'line': self.line_number,
                        'type': 'syntax_error',
                        'message': f'{keyword.strip()} statement must end with colon',
                        'severity': 'error'
                    })
                break

# --- Secure Python Executor (modified for WebSockets) ---

class SecurePythonExecutor:
    """Secure Python code executor with sandboxing and timeout, now interactive."""
    
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.restricted_modules = {
            'os', 'sys', 'subprocess', 'socket', 'urllib', 'requests',
            'shutil', 'glob', 'pickle', 'marshal', 'shelve', 'dbm',
            'sqlite3', 'threading', 'multiprocessing', 'ctypes', 'importlib',
            'builtins', '__builtin__', 'imp', 'zipimport'
        }
        
        # Create safe builtins
        # IMPORTANT: 'input' is now included and will be redirected!
        self.safe_builtins = {
            'print': print,
            'len': len,
            'range': range,
            'str': str,
            'int': int,
            'float': float,
            'bool': bool,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,
            'frozenset': frozenset,
            'abs': abs,
            'max': max,
            'min': min,
            'sum': sum,
            'sorted': sorted,
            'reversed': reversed,
            'enumerate': enumerate,
            'zip': zip,
            'map': map,
            'filter': filter,
            'all': all,
            'any': any,
            'type': type,
            'isinstance': isinstance,
            'hasattr': hasattr,
            'getattr': getattr,
            'setattr': setattr,
            'delattr': delattr,
            'round': round,
            'pow': pow,
            'divmod': divmod,
            'chr': chr,
            'ord': ord,
            'hex': hex,
            'oct': oct,
            'bin': bin,
            'format': format,
            'repr': repr,
            'ascii': ascii,
            'iter': iter,
            'next': next,
            'slice': slice,
            'callable': callable,
            'id': id,
            'hash': hash,
            'vars': vars,
            'dir': dir,
            'help': help,
            'Exception': Exception,
            'ValueError': ValueError,
            'TypeError': TypeError,
            'IndexError': IndexError,
            'KeyError': KeyError,
            'AttributeError': AttributeError,
            'NameError': NameError,
            'ZeroDivisionError': ZeroDivisionError,
            'input': input # Now included for interactive use
        }
        
    async def execute_code(self, code: str, websocket_io: WebSocketInputOutput) -> Dict[str, Any]:
        """Execute Python code in a secure environment with WebSocket IO."""
        start_time = time.time()
        
        # Input validation
        if not code or not code.strip():
            error_msg = 'No code provided'
            await websocket_io.write(f"Error: {error_msg}\n")
            return {
                'success': False,
                'output': '',
                'error': error_msg,
                'execution_time': 0,
                'validation': {
                    'valid': False,
                    'errors': [{'message': error_msg, 'line': 0}],
                    'warnings': [],
                    'total_issues': 1
                }
            }
        
        # Pre-execution validation
        fsm = PythonSyntaxFSM()
        validation_result = fsm.validate_code(code)
        
        # Check for restricted imports
        restricted_check = self.check_restricted_imports(code)
        if not restricted_check['allowed']:
            error_msg = f"Security violation: Restricted module '{restricted_check['module']}' is not allowed"
            await websocket_io.write(f"Error: {error_msg}\n")
            return {
                'success': False,
                'output': '',
                'error': error_msg,
                'execution_time': 0,
                'validation': validation_result
            }
        
        # Execute code with timeout
        result = await self.run_with_timeout(code, websocket_io) # Pass websocket_io
        execution_time = time.time() - start_time
        
        return {
            'success': result['success'],
            'output': result['output'],
            'error': result['error'],
            'execution_time': round(execution_time, 3),
            'validation': validation_result
        }
    
    def check_restricted_imports(self, code: str) -> Dict[str, Any]:
        """Check for restricted module imports"""
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split('.')[0] in self.restricted_modules:
                            return {'allowed': False, 'module': alias.name}
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split('.')[0] in self.restricted_modules:
                        return {'allowed': False, 'module': node.module}
            return {'allowed': True, 'module': None}
        except Exception:
            return {'allowed': True, 'module': None}   # If parsing fails, let execution handle it
    
    async def run_with_timeout(self, code: str, websocket_io: WebSocketInputOutput) -> Dict[str, Any]:
        """Run code with timeout protection, interacting via WebSocketIO."""
        result = {'success': False, 'output': '', 'error': ''}
        
        # Store original stdin/stdout/stderr
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        # Use a threading.Event to signal completion from the execution thread
        execution_finished_event = threading.Event()

        def target():
            nonlocal result
            try:
                # Redirect sys.stdin, sys.stdout, sys.stderr for this thread
                sys.stdin = websocket_io
                sys.stdout = websocket_io
                sys.stderr = websocket_io

                # Create restricted execution environment
                restricted_globals = {
                    '__builtins__': self.safe_builtins,
                    '__name__': '__main__',
                    '__doc__': None,
                }
                
                # Execute the code
                exec(code, restricted_globals)
                result['success'] = True
                
            except EOFError as e:
                # This happens if the WebSocket disconnects during input()
                result['error'] = f"Input stream closed: {str(e)}"
                result['success'] = False
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
                # Write error to WebSocket and internal buffer
                try:
                    asyncio.run(websocket_io.write(f"Error: {error_msg}\n"))
                except Exception as write_e:
                    print(f"Failed to write error to WebSocket: {write_e}")
                websocket_io.error_buffer.write(error_msg)
                result['success'] = False
            finally:
                # Restore original stdin/stdout/stderr for the thread pool (if applicable)
                sys.stdin = original_stdin
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                execution_finished_event.set() # Signal completion

        # Run the execution in a separate thread to allow for timeout
        thread = threading.Thread(target=target)
        thread.daemon = True # Allow the program to exit even if thread is running
        thread.start()

        # Wait for the thread to finish or timeout
        thread.join(timeout=self.timeout)
        
        if thread.is_alive():
            result['error'] = f"Code execution timed out after {self.timeout} seconds"
            result['success'] = False
            # Attempt to send timeout message to client
            try:
                await websocket_io.write(f"Error: {result['error']}\n")
            except Exception:
                pass # Ignore if websocket is already closed
        else:
            # Execution finished within timeout
            result['output'] = websocket_io.output_buffer.getvalue()
            if websocket_io.error_buffer.getvalue():
                result['error'] = websocket_io.error_buffer.getvalue()
                result['success'] = False
            else:
                result['success'] = True
        
        # Send a final message to the client indicating execution is complete
        try:
            await websocket_io.send_text(json.dumps({"type": "execution_complete", "result": result}))
        except WebSocketDisconnect:
            pass # Ignore if websocket is already closed
        except Exception as e:
            print(f"Error sending execution_complete message: {e}")

        return result


# Initialize the executor
executor = SecurePythonExecutor(timeout=10)

# Initialize FastAPI app
app = FastAPI(
    title="Interactive Python Code Executor API",
    description="A secure WebSocket API for executing Python code interactively.",
    version="1.0.0",
)

# Serve the HTML frontend
@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serves the interactive frontend."""
    # This assumes index.html is in the same directory as app.py
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read())

# WebSocket endpoint for interactive execution
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket_io = WebSocketInputOutput(websocket)
    print(f"WebSocket connection established: {websocket.client}")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data["type"] == "code":
                code = data["content"]
                print(f"Received code from {websocket.client}: {code[:50]}...")
                # Execute code asynchronously in the background
                asyncio.create_task(executor.execute_code(code, websocket_io))
            elif data["type"] == "input":
                user_input = data["content"]
                print(f"Received input from {websocket.client}: {user_input.strip()[:50]}...")
                await websocket_io.input_queue.put(user_input + "\n") # Add newline for readline
            elif data["type"] == "ping":
                # Respond to pings to keep connection alive
                await websocket.send_text(json.dumps({"type": "pong"}))
            else:
                await websocket_io.write(f"Unknown message type: {data['type']}\n")

    except WebSocketDisconnect:
        print(f"WebSocket disconnected: {websocket.client}")
    except json.JSONDecodeError:
        print(f"Received invalid JSON from {websocket.client}: {message}")
    except Exception as e:
        print(f"WebSocket error for {websocket.client}: {e}")
    finally:
        websocket_io.closed = True
        # Clean up any pending input requests if the client disconnects
        while not websocket_io.input_queue.empty():
            try:
                websocket_io.input_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        print(f"WebSocket connection closed for {websocket.client}")

# This block is for local development using `uvicorn`
# Hugging Face Spaces will use `uvicorn` directly via the Dockerfile CMD
if __name__ == "__main__":
    import uvicorn
    print("Starting Interactive Python Code Executor API (FastAPI)...")
    print(f"Python version: {sys.version}")
    print(f"Working directory: {os.getcwd()}")
    
    server_name = os.getenv('FASTAPI_SERVER_NAME', '0.0.0.0')
    server_port = int(os.getenv('FASTAPI_SERVER_PORT', 7860))
    
    print(f"Starting server on {server_name}:{server_port}")
    
    try:
        uvicorn.run(
            "app:app",  # app:app means the `app` object in `app.py`
            host=server_name,
            port=server_port,
            reload=False, # Set to True for local dev to auto-reload on code changes
            log_level="info"
        )
    except Exception as e:
        print(f"Failed to start server: {e}")
        sys.exit(1)

