# compression-example

## Purpose

This application demonstrates how to the Valkey-GLIDE client, compress data in a Python app, then save it to Valkey (or Redis OSS), and show the compression results.

## Benefits

By using compression in your application, you can save significant space. Since Valkey and Redis OSS are both in-memory data stores, memory can be consumed quickly, especially with very large key values.

This code shows a simple way of compressing string values and saving them. Depending on the number of keys and words you specify in parameters, you can see anywhere from 20% to 50% compression.

## Requirements

The following libraries, defined in `requirements.txt`, are required:

- lz4
- valkey-glide
- numpy

## Usage

Run with the `-h` flag to see the options:

```text
./app.py -h
```

Output:

```text
usage: app.py [-h] [-k NUM_OF_KEYS] [-w NUM_OF_WORDS] [-e ENDPOINT] [-p N] [-c] [-s]
              [-a] [-v]

Runs a quick data compression test on strint values for Valkey or Redis OSS.

options:
  -h, --help            show this help message and exit
  -k NUM_OF_KEYS, --keys NUM_OF_KEYS
                        Number of keys to write.
  -w NUM_OF_WORDS, --words NUM_OF_WORDS
                        Number of words per key.
  -e ENDPOINT, --endpoint ENDPOINT
                        Endpoint to connect to. Defaults to localhost.
  -p N, --port N        Port to connect to. Defaults to 6379.
  -c, --cluster-mode-enabled
                        Use cluster-mode-enabled (CME). Default is False.
  -s, --ssl             Enable encryption-in-transit.
  -a, --auth            If enabled, USER_NAME and USER_PASS env variables are used to
                        authenticate.
  -v, --verbose         Verbose debug output.
```

## Example

Command to launch against a Valkey or Redis OSS cluster with TLS enabled, providing a username and password, and in cluster mode.

```text
export USER_NAME="my_valkey_or_redis_user"
export USER_PASS="my_valkey_or_redis_password"
./app.py -e my_cluster_dns_name -k 1000 -w 400 -s -a -c
```

Output:

```text
Testing connection to my_cluster_dns_name:6379 - please wait...
Successful connection!

NOTE! Remember to delete keys with the prefix of 'testkeys:' after running this test!

RESULTS:

       Total raw bytes written: 2,147,118
Total compressed bytes written: 1,186,749

     Average compression ratio: 0.4471723474195586

Executed in 2.21 seconds
```

