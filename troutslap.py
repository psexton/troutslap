import logging
import os

from flask import abort, Flask, jsonify, request

#
# App setup & boilerplate
#

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
app = Flask(__name__)


@app.route('/status', methods=['GET'])
def status():
    return jsonify(status='OK')


def is_request_valid(request):
    is_token_valid = request.form['token'] == os.environ['SLACK_VERIFICATION_TOKEN']
    is_team_id_valid = request.form['team_id'] == os.environ['SLACK_TEAM_ID']

    return is_token_valid and is_team_id_valid


@app.route('/hook', methods=['POST'])
def hello_there():
    if not is_request_valid(request):
        abort(400)

    return jsonify(
        response_type='in_channel',
        text='Happy slapping!',
    )
