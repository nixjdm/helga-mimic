import markovify
import os
import os.path
import re
import requests
import time

from helga import settings, log
from helga.db import db
from helga.plugins import Command, ResponseNotReady

from helga_alias import find_alias, is_alias
from helga_twitter import tweet

from cobe.brain import Brain
from twisted.internet import reactor, threads

ADDRESSING_POSSIBLE_NICK = re.compile(r'(?:^|(?:[.!?]\s))(\w+)')

DEBUG = getattr(settings, 'HELGA_DEBUG', False)
GENERATE_TRIES = int(getattr(settings, 'MIMIC_GENERATE_TRIES', 50))
IGNORED = getattr(settings, 'IGNORED', [])
NICK = getattr(settings, 'NICK')
OPS = getattr(settings, 'OPERATORS', [])
STATE_SIZE = int(getattr(settings, 'MIMIC_STATE_SIZE', 2))
THINK_TIME = int(getattr(settings, 'MIMIC_THINK_TIME', 2000))

logger = log.getLogger(__name__)

# API
def bot_say(seed='', think_time=THINK_TIME):
    """

    Generate response from cobe, seeding with the message.

    The we do some processing on the out, like removing nicks (both
    active and known), or replacing nick mentions with OP or preset
    list.

    1. remove nicks (both active and known), replace with either OP or
    something from a preset list TODO

    2. remove odd number quotes (the first)

    3. TODO
    """

    response = Brain('brain.ai').reply(
        seed.replace(NICK,''),
        loop_ms=think_time,
    )



    balance_chars = ['"', '\'']
    remove_chars = ['[', ']', '{', '}', '(', ')']

    for char in remove_chars:
        response = response.replace(char, '')

    for char in balance_chars:
        if response.count(char) % 2:
            response = response.replace(char, '', 1)

    return response



class MimicPlugin(Command):

    command = 'mimic'
    last_response = ''

    def generate_sentence(self, channel_or_nicks):
        """
        Generates a sentence from the corpus of `channel_or_nick`
        """

        logger.debug('generating sentence for {}'.format(channel_or_nicks))

        models = []

        for nick in channel_or_nicks:
            _, aliases = find_alias(nick, create_new=False)
            for alias in aliases:
                filename = 'markov-{}.json'.format(alias)
                if os.path.exists(filename):
                    with open(filename, 'r') as f:
                        models.append(markovify.NewlineText.from_json(f.read()))

        if models:
            return markovify.combine(models).make_sentence(
                tries=GENERATE_TRIES
            )

    def train_brain(self, channel):
        """
        create a cobe brain file based on the db.

        This file is used by cobe to generate responses.
        """

        logger.debug('starting training')
        logger.debug('ignored: {}'.format(IGNORED))

        # replace the current brain
        try:
            os.remove('brain.ai')
        except:
            pass

        BRAIN = Brain('brain.ai')

        logger.debug('created brain.ai')

        start = time.time()

        BRAIN.start_batch_learning()

        logger_lines = db.logger.find({
            'channel': channel,
            'nick': {'$nin': IGNORED},
            'message': {'$regex': '^(?!\.|\,|\!)'},
        })

        logger.debug('log total: {}'.format(logger_lines.count()))

        for line in logger_lines:
            BRAIN.learn(line['message'])

        BRAIN.stop_batch_learning()

        logger.debug('learned stuff. Took {:.2f}s'.format(
            time.time() - start
        ))


    def generate_model(self, client, channel_or_nick, corpus=''):
        """
        Generates a markov chain for channel or nick.
        """

        logger.debug('generating model for {}'.format(channel_or_nick))

        db_filter = {
            'nick': {'$nin': IGNORED},
            'message': {'$regex': '^(?!\.|\,|\!)'},
        }

        if client.is_public_channel(channel_or_nick):
            self.train_brain(channel_or_nick)
            db_filter['channel'] = channel_or_nick
        else:
            db_filter['nick'] = channel_or_nick

        logger.debug('{} lines found'.format(db.logger.find(db_filter).count()))


        if not corpus:
            for doc in db.logger.find(db_filter):
                corpus += doc['message']
                corpus += '\n'

        markov_chain = markovify.NewlineText(corpus, state_size=STATE_SIZE)

        with open('markov-{}.json'.format(channel_or_nick), 'w') as f:
            f.write(markov_chain.to_json())

        logger.debug('done creating markov file.')

    def generate_models(self, client, channel, channel_or_nicks):
        """
        Create markov files for each nick specified, or the channel.
        """

        if not channel_or_nicks:
            # build models for every nick, and cobe brain
            logger.debug('building all nicks')

            nicks_pipeline = [
                {'$match': {
                    'channel': channel,
                }},
                {'$group': {
                    '_id': '$nick'
                }},
            ]

            results = [nick for nick in db.logger.aggregate(nicks_pipeline)]
            channel_or_nicks = [nick['_id'] for nick in results]
            channel_or_nicks.append(channel)

        for channel_or_nick in channel_or_nicks:
            self.generate_model(client, channel_or_nick)

        client.msg(channel, 'build done!')

    def preprocess(self, client, channel, nick, message):
        """
        listen out for our nick. if mentioned, we'll respond with a
        (hopefully) relevant statement.
        """

        if NICK in message and not message.startswith(
                getattr(settings, 'COMMAND_PREFIX_CHAR')
        ):
            response = bot_say(seed=message)
            potential_nick_matches = ADDRESSING_POSSIBLE_NICK.match(response)

            if potential_nick_matches:
                potential_nick = potential_nick_matches.groups()[0]

                if is_alias(potential_nick):
                    response = response.replace(potential_nick, nick)

            client.msg(channel, response)
            self.last_response = response

        return channel, nick, message

    def process_build_error(self, result):
        logger.debug('got error while building models: {}'.format(result))

    def run(self, client, channel, nick, message, cmd, args):

        logger.debug('args: {}'.format(args))
        logger.debug('cmd: {}'.format(cmd))

        if not args:
            args = [channel]

        channel_or_nicks = args

        if 'tweet' in channel_or_nicks:
            logger.debug(u'sending tweet: {}'.format(self.last_response))
            reactor.callLater(0, tweet, client, channel, self.last_response)
            raise ResponseNotReady

        if 'build' in channel_or_nicks:

            deferred_build = threads.deferToThread(
                self.generate_models,
                client, channel, channel_or_nicks[1:]
            )

            deferred_build.addErrback(self.process_build_error)

            raise ResponseNotReady

        if 'load' in channel_or_nicks:

            if len(args) < 3:
                return 'usage: !mimic load <key> <url>'

            key = str(args[1])
            resp = requests.get(args[2])

            self.generate_model(client, key, corpus=resp.content)

            return 'Done!'

        start = time.time()
        generated = self.generate_sentence(channel_or_nicks)
        duration = time.time() - start

        if not generated:
            return 'i got nothing :/'

        self.last_response = generated

        if DEBUG:
            generated = u"{} [{:.2f}s]".format(generated, duration)

        return generated
