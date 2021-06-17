# <editor-fold desc="AGPLv3 preamble">
# troutslap
# Copyright (C) 2021  Paul Sexton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# </editor-fold>

import boto3
import hashlib
import hmac
import json
import logging
import os
import random
import re
from time import sleep

from flask import abort, Flask, jsonify, request, redirect
import requests
from zappa.asynchronous import task

#
# App setup & boilerplate
#

# pull configs and secrets from aws param store
# do this once at global level hopefully
ssm = boto3.client('ssm')
client_id = ssm.get_parameter(Name='/slackapp/troutslap/client_id', WithDecryption=False)['Parameter']['Value']
client_secret = ssm.get_parameter(Name='/slackapp/troutslap/client_secret', WithDecryption=True)['Parameter']['Value']
signing_secret = ssm.get_parameter(Name='/slackapp/troutslap/signing_secret', WithDecryption=True)['Parameter']['Value']
# initialize dynamodb table
installations = boto3.resource('dynamodb').Table('troutslap-installations')

DEBUG_MODE = os.getenv('TROUTSLAP_DEBUG', 'False').lower() in ('true', '1')

logger = logging.getLogger()
logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
app = Flask(__name__)


@app.route('/status', methods=['GET'])
def status():
    return jsonify(status='OK', debug_mode=DEBUG_MODE)


# Simple route for redirecting the user to slack to install this app in a workspace
@app.route('/install')
def install():
    scopes = "commands,chat:write,chat:write.public"
    return redirect(f"https://slack.com/oauth/v2/authorize?scope={scopes}&client_id={client_id}")


# Callback from install to exchange secret code for oauth token
@app.route('/oauth2_redirect')
def authorize():
    form_data = {"client_id": client_id, "client_secret": client_secret, "code": request.args.get('code')}
    response = requests.post('https://slack.com/api/oauth.v2.access', data=form_data)
    response_body = json.loads(response.text)
    if response_body["ok"]:

        # if successful, store in DB and return happy response
        team_id = response_body["team"]["id"]
        team_name = response_body["team"]["name"]
        token = response_body["access_token"]
        store_token(team_id, team_name, token)
        logger.info(f"event=install status=success team_id={team_id}")
        return f"Successfully installed to {team_name}!"
    else:
        # if not successful, return unhappy response
        logger.error(f"event=install status=fail slack response={response}")
        return "Installation failed", response.status_code


@app.route('/hook', methods=['POST'])
def slap():
    raw_body = request.get_data()
    data = request.form
    logger.debug(f"data={data}")
    if not is_request_valid(body=raw_body, timestamp=request.headers['X-Slack-Request-Timestamp'],
                            signature=request.headers['X-Slack-Signature']):
        logger.warning("event=hook status=fail invalid request")
        abort(400)

    team_id = data['team_id']

    # Check for help invocation
    if data['text'] == "help":
        logger.info(f"event=hook status=success team_id={team_id} type=help")
        return jsonify(
            response_type="ephemeral",
            text="@ mention one or more other users or bots to engage them in combat.\n"
                 "Example usages: `/slap @larry`, `/slap @curly @moe` "
        )
    # Check if the user used @here, @channel, or @everyone
    # Chastise them and suppress the @-ing from the channel
    elif mass_at_mention(data['text']):
        logger.info(f"event=hook status=success team_id={team_id} type=mass")
        return jsonify(
            response_type="ephemeral",
            text="You don't stand a chance fighting that many people."
        )
    else:
        # parse out who slapped and who is getting slapped
        initiator = data['user_id']
        involved = involved_users(data)
        logger.debug(f"initiator={initiator}")
        logger.debug(f"involved={involved}")

        if len(involved) == 1:
            # if they're alone, handle that special case too
            logger.info(f"event=hook status=success team_id={team_id} type=self")
            return jsonify(
                response_type='ephemeral',
                text="No one else is around. You slap yourself. The fish wins."
            )
        else:
            # otherwise, handle the normal case
            logger.debug("queuing normal slap")
            give_em_the_slaps(data['team_id'], data['channel_id'], initiator, involved)

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
    signing_secret_bytes = bytes(signing_secret, 'utf-8')

    # Compute the HMAC signature
    computed_signature = 'v0=' + hmac.new(signing_secret_bytes, basestring, hashlib.sha256).hexdigest()

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
def give_em_the_slaps(team_id, channel_id, initiator, players):
    INITIAL_PAUSE_DURATION = 0.5
    PAUSE_DURATION = 1

    # Sometimes, our first post beats the user's post to visibility in the channel, and that looks weird
    # So wait a little bit
    if not DEBUG_MODE:
        sleep(INITIAL_PAUSE_DURATION)

    # Get the content we'll be using
    messages = write_messages(initiator, players)
    logger.info(f"event=hook status=success team_id={team_id} type=normal length={len(messages)} players={len(players)}")

    # Look up the token for this team
    oauth_token = load_token(team_id)

    # Send the content to slack
    for message in messages:
        response = {'channel': channel_id, 'text': message}
        logger.debug(f"posting {response['text']}")
        response = requests.post("https://slack.com/api/chat.postMessage",
                                 json=response,
                                 headers={'Authorization': f"Bearer {oauth_token}"})
        logger.debug(f"response.status_code={response.status_code}")
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


# write a team's auth info out to dynamodb
def store_token(team_id, team_name, token) -> None:
    logger.debug(f"storing token for team_id={team_id}, team_name={team_name}")
    # this will add a new item or overwrite an existing item
    installations.put_item(
        Item={
            'team_id': team_id,
            'team_name': team_name,
            'access_token': token
        }
    )


# read in a team's auth info from dynamodb
def load_token(team_id) -> str:
    response = installations.get_item(
        Key={
            'team_id': team_id
        }
    )
    if "Item" in response:
        token = response['Item']['access_token']
        return token
    else:
        raise RuntimeError(f"No token found for team_id={team_id}")
