import os
from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

FLASK_PORT = os.getenv("FLASK_PORT")
app = Flask(__name__)

@app.route('/health')
def health_check():
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=FLASK_PORT)