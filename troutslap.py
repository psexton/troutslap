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
        logger.debug(
            f"Signature verification failed. basestring={basestring} received={signature}, computed={computed_signature}")
        return False


def mass_at_mention(text):
    # slack encoded the three special @-mentions with a ! not a @
    return "!here" in text or "!channel" in text or "!everyone" in text


@app.route('/hook', methods=['POST'])
def slap():
    raw_body = request.get_data()
    form_data = request.form
    if not is_request_valid(body=raw_body, timestamp=request.headers['X-Slack-Request-Timestamp'],
                            signature=request.headers['X-Slack-Signature']):
        logging.warning('invalid request')
        abort(400)

    # Check if the user used @here, @channel, or @everyone
    # Chastise them and suppress the @-ing from the channel
    if mass_at_mention(form_data['text']):
        logging.info("mass slap attempted")
        return jsonify(
            response_type="ephemeral",
            text="You don't stand a chance fighting that many people."
        )
    else:
        return jsonify(
            response_type='in_channel',
            text='Happy slapping!',
        )
