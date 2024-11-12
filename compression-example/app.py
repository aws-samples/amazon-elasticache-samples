#!/usr/bin/env python3
import asyncio
import lz4.frame
import random
import argparse
import sys
import time
import os
import numpy
from glide import ReadFrom, GlideClusterClientConfiguration, GlideClientConfiguration, NodeAddress, GlideClusterClient, GlideClient, ServerCredentials

USER_NAME = os.getenv('USER_NAME', 'default')
USER_PASS = os.getenv('USER_PASS')
key_prefix = "testkeys:"

WORDS = [
    "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "and", "runs",
    "away", "from", "the", "village", "on", "a", "sunny", "day", "in", "the",
    "beautiful", "forest", "with", "tall", "trees", "and", "colorful", "flowers"
]

parser = argparse.ArgumentParser(description='Runs a quick data compression test on strint values for Valkey or Redis OSS.')

parser.add_argument('-k', '--keys', metavar='NUM_OF_KEYS', default=100, help='Number of keys to write.')
parser.add_argument('-w', '--words', metavar='NUM_OF_WORDS', default=200, help='Number of words per key.')
parser.add_argument('-e', '--endpoint', default='localhost', help='Endpoint to connect to. Defaults to localhost.')
parser.add_argument('-p', '--port', metavar='N', default=6379, help='Port to connect to. Defaults to 6379.')
parser.add_argument('-c', '--cluster-mode-enabled', action='store_true', help='Use cluster-mode-enabled (CME). Default is False.')
parser.add_argument('-s', '--ssl', help='Enable encryption-in-transit.', action='store_true')
parser.add_argument('-a', '--auth', action='store_true', help='If enabled, USER_NAME and USER_PASS env variables are used to authenticate.')
parser.add_argument('-v', '--verbose', help='Verbose debug output.', action='store_true')

args=parser.parse_args()


class Connection:

    async def setup(self, args):

        try:
            
            print ('')
            print ('Testing connection to %s:%s - please wait...' % (args.endpoint, args.port))
            self.addresses = [NodeAddress (args.endpoint, int(args.port))]
            self.credentials = None
            
            if USER_PASS is not None:
                
                self.credentials = ServerCredentials (USER_PASS, USER_NAME)
                
            else:
                
                self.credentials = ServerCredentials (USER_NAME)

            if args.cluster_mode_enabled:

                if args.auth:

                    self.config = GlideClusterClientConfiguration(self.addresses, read_from=ReadFrom.PRIMARY, use_tls=args.ssl, credentials=self.credentials)

                else: 

                    self.config = GlideClusterClientConfiguration(self.addresses, read_from=ReadFrom.PRIMARY, use_tls=args.ssl)

            else:

                if args.auth:

                    self.config = GlideClientConfiguration(self.addresses, read_from=ReadFrom.PRIMARY, use_tls=args.ssl, credentials=self.credentials)

                else:
                                    
                    self.config = GlideClientConfiguration(self.addresses, read_from=ReadFrom.PRIMARY, use_tls=args.ssl)

            self.connection = await GlideClusterClient.create(self.config) if args.cluster_mode_enabled else await GlideClient.create(self.config)   
            response = await self.connection.ping()
            response_text = response.decode('UTF-8')
            
            if response_text == "PONG":
                
                print ('Successful connection!')
                print ('')

        except Exception as e:
                    
            print(type(e).__name__)
            print(e)
            sys.exit(1)

    def get_connection(self):

        return self.connection


async def write_key(client, key_name, key_value):

    while True:

        try:

            await client.set(key_name, key_value)
            
            if args.verbose:
                
                print('Successful SET key %s to %s' % (key_name, key_value))

        except Exception as e:

            print(type(e).__name__)
            print(e)
            continue

        break


async def start_test(client):

    print('')
    print ("NOTE! Remember to delete keys with the prefix of '%s' after running this test!" % key_prefix)

    ratios = []
    num = 0
    total_raw_bytes = 0
    total_compressed_bytes = 0
    
    for num in range (1, int(args.keys) + 1):
        
        key_name = key_prefix + str(num)

        # create a random string of words
        sentence_length = random.randint(int(args.words) -1, int(args.words) + 1)
        key_value = ' '.join(random.choice(WORDS) for _ in range(sentence_length))
        total_raw_bytes += sys.getsizeof(key_value)
        
        # compress the string
        compressed_value = lz4.frame.compress(key_value.encode())
        total_compressed_bytes += sys.getsizeof(compressed_value)
        
        #get original size and compressed size
        original_size = sys.getsizeof(key_value)
        compressed_size = sys.getsizeof(compressed_value)
        ratio = 1 - (compressed_size / original_size)
        # add the ratio to a numpy array
        ratios.append(ratio)

        await write_key(client, key_name + ":original", key_value)
        await write_key(client, key_name + ":compressed", compressed_value)
        
    raw_formatted = "{:,}".format(total_raw_bytes)
    compressed_formatted = "{:,}".format(total_compressed_bytes)
    
    print ('')
    print ('RESULTS:')
    print ('')
    print ('       Total raw bytes written: ' + raw_formatted)
    print ('Total compressed bytes written: ' + compressed_formatted)
    print ('')
    print ('     Average compression ratio: %s' % numpy.average(ratios))
    print ('')
    
    if (numpy.average(ratios) < .30):
        
        print ('Compression ratio is less than 30%. Try a different number')
        print ('of words or keys to increase compression ratio.')
        print ('')


def handle_exception(loop, context):

    exception = context.get('exception')
    
    if isinstance(exception, asyncio.CancelledError):
        
        return


async def main():

    conn = Connection()
    await conn.setup(args)
    client = conn.get_connection()
    start_time = time.time()
    await start_test(client)
    print(f"Executed in {time.time() - start_time:.2f} seconds", flush=True)
    print('')
    pass


loop = asyncio.get_event_loop()
loop.set_exception_handler(handle_exception)
loop.run_until_complete(main())

