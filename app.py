from flask import Flask, request, jsonify, render_template_string
import subprocess
import uuid
import os

app = Flask(__name__)

# Serve index.html directly
with open("index.html", "r", encoding="utf-8") as f:
    html_template = f.read()

@app.route('/')
def index():
    return render_template_string(html_template)

@app.route('/run', methods=['POST'])
def run_python():
    data = request.get_json()
    code = data.get('code', '')
    user_input = data.get('input', '')

    if not code.strip():
        return jsonify({'error': 'No code provided.'})

    temp_id = str(uuid.uuid4())[:8]
    code_path = f'temp_{temp_id}.py'

    with open(code_path, 'w', encoding='utf-8') as f:
        f.write(code)

    try:
        command = [
            "docker", "run", "-i", "--rm",
            "-v", f"{os.path.abspath(code_path)}:/app/script.py",
            "-w", "/app",
            "python:3.10", "python", "script.py"
        ]

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        output, error = process.communicate(input=user_input)

        return jsonify({'output': output, 'error': error})

    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        if os.path.exists(code_path):
            os.remove(code_path)

if __name__ == '__main__':
    app.run(debug=True)
