import hashlib
import hmac
import logging
import os
import random
import re
from time import sleep

from flask import abort, Flask, jsonify, request
import requests
from zappa.asynchronous import task

#
# App setup & boilerplate
#

DEBUG_MODE = True

logger = logging.getLogger()
logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.info)
app = Flask(__name__)


@app.route('/status', methods=['GET'])
def status():
    return jsonify(status='OK')


@app.route('/hook', methods=['POST'])
def slap():
    raw_body = request.get_data()
    data = request.form
    logging.debug(f"data={data}")
    if not is_request_valid(body=raw_body, timestamp=request.headers['X-Slack-Request-Timestamp'],
                            signature=request.headers['X-Slack-Signature']):
        logging.warning('invalid request')
        abort(400)

    # Check for help invocation
    if data['text'] == "help":
        logging.info("help requested")
        return jsonify(
            response_type="ephemeral",
            text="Example usages: `/slap @larry`, `/slap @curly @moe`"
        )
    # Check if the user used @here, @channel, or @everyone
    # Chastise them and suppress the @-ing from the channel
    elif mass_at_mention(data['text']):
        logging.info("mass slap attempted")
        return jsonify(
            response_type="ephemeral",
            text="You don't stand a chance fighting that many people."
        )
    else:
        # parse out who slapped and who is getting slapped
        initiator = data['user_id']
        involved = involved_users(data)
        logging.debug(f"initiator={initiator}")
        logging.debug(f"involved={involved}")

        if len(involved) == 1:
            # if they're alone, handle that special case too
            logging.info("self slap attempted")
            return jsonify(
                response_type='ephemeral',
                text="No one else is around. You slap yourself. The fish wins."
            )
        else:
            # otherwise, handle the normal case
            logging.info("queuing normal slap")
            give_em_the_slaps(data['channel_id'], initiator, involved)

            # return immediately with empty response
            # Need to include the "in_channel" response_type so that the user's
            # command invocation is shown to the channel.
            # See <https://api.slack.com/interactivity/slash-commands#best_practices>
            return jsonify(response_type='in_channel')


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


def involved_users(form):
    # This regex will match either <@XXX> or <@XXX|YYY>, and return just the XXX part.
    # We don't know what either string may contain, so match from "<@" to either "|" or ">".
    # See https://api.slack.com/changelog/2017-09-the-one-about-usernames
    pattern = "<@([^|>]+)"
    mentioned = re.findall(pattern, form['text'])

    # also include the person who sent the command
    initiator = form['user_id']
    involved = mentioned + [initiator]

    # dedup in case they mentioned themselves, or mentioned someone more than once
    temp_set = set(involved)
    involved = list(temp_set)

    return involved


@task  # run this async in the background
def give_em_the_slaps(channel_id, initiator, players):
    INITIAL_PAUSE_DURATION = 0.5
    PAUSE_DURATION = 1

    # Sometimes, our first post beats the user's post to visibility in the channel, and that looks weird
    # So wait a little bit
    if not DEBUG_MODE:
        sleep(INITIAL_PAUSE_DURATION)

    # Get the content we'll be using
    messages = write_messages(initiator, players)

    # Send the content to slack
    for message in messages:
        response = {'channel': channel_id, 'text': message}
        logging.debug(f"posting {response['text']}")
        response = requests.post("https://slack.com/api/chat.postMessage",
                                 json=response,
                                 headers={'Authorization': 'Bearer {}'.format(os.environ['SLACK_OAUTH_TOKEN'])})
        logging.debug(f"response.status_code={response.status_code}")
        if not DEBUG_MODE:
            sleep(PAUSE_DURATION)


def write_messages(initiator, players):
    MIN_SLAPS = 1
    MAX_SLAPS = 9
    FISH_NAMES = ["trout", "pair of sardines", "salmon", "barramundi", "sturgeon", "ocean sunfish", "shark", "guppy"]

    messages = []

    # Slap a few times
    num_slaps = random.randint(MIN_SLAPS, MAX_SLAPS)
    slapper = initiator  # The first slapper is always the initiator
    for i in range(num_slaps):
        # slappee can't be the slapper
        possible_slapees = [player for player in players if player != slapper]
        slappee = random.choice(possible_slapees)
        fish = random.choice(FISH_NAMES)
        message = f'{encode_name(slapper)} slaps {encode_name(slappee)} with a {fish}'
        messages.append(message)
        slapper = slappee  # The slappee becomes the slapper in the next round

    # Pick a winner
    winner = random.choice(players)
    final_message = f'{encode_name(winner)} wins!'
    messages.append(final_message)
    return messages


def encode_name(user_id):
    return f'<@{user_id}>'
