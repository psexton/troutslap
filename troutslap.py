import hashlib
import hmac
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


def is_request_valid(body, timestamp, signature):
    # Decode the bytes in the raw request body, assemble the base string, and re-encode as bytes
    body_str = body.decode('utf-8')
    basestring = f"v0:{timestamp}:{body_str}".encode('utf-8')

    # encode the signing secret as bytes too
    signing_secret = bytes(os.environ['SLACK_SIGNING_SECRET'], 'utf-8')

    # Compute the HMAC signature
    computed_signature = 'v0=' + hmac.new(signing_secret, basestring, hashlib.sha256).hexdigest()

    # Compare it to what we received with the request
    if hmac.compare_digest(computed_signature, signature):
        return True
    else:
        logger.debug(f"Signature verification failed. basestring={basestring} received={signature}, computed={computed_signature}")
        return False


@app.route('/hook', methods=['POST'])
def slap():
    raw_body = request.get_data()
    if not is_request_valid(body=raw_body, timestamp=request.headers['X-Slack-Request-Timestamp'],
                            signature=request.headers['X-Slack-Signature']):
        logging.warning('invalid request')
        abort(400)

    return jsonify(
        response_type='in_channel',
        text='Happy slapping!',
    )
