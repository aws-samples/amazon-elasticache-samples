#docker run --rm --network container:aerospike   aerospike/aerospike-tools  asbench  -h 127.0.0.1  -p 3000  -n test -s million_data -k 50 -w I -o "I8,S40,D,[2*I4],{S10:I2,S10:I2}"  --batch-write-size 20000  --start-key 1  -t 0  -R  -z 8
# Load the comprehensive data
docker run --rm --network container:aerospike \
  aerospike/aerospike-tools \
  asbench -h 127.0.0.1 -p 3000 -n test -s all_datatypes -k 100 \
  -o "I8,S40,D,B100,[5*I4],[3*S20],{S10:I4,S10:S30,S10:D},{S15:{S5:I2,S5:S10}},[[2*I4],[3*S10]],[5*S5],B512,{S20:B50}" \
  --start-key 1
# Migrate with all features

# ### **Simple Data Types:**
# • **I8** - 8-byte integer
# • **S40** - 40-character string
# • **D** - Double/float value
# • **B100** - 100 bytes of binary data

# ### **Lists:**
# • **[5*I4]** - List containing 5 integers (4-byte each)
# • **[3*S20]** - List containing 3 strings (20 characters each)
# • **[[2*I4],[3*S10]]** - Nested list: outer list containing two inner lists
#   • First inner list: 2 integers
#   • Second inner list: 3 strings (10 chars each)
# • **[5*S5]** - List containing 5 short strings (5 characters each)

# ### **Maps (Dictionaries):**
# • **{S10:I4,S10:S30,S10:D}** - Map with 3 key-value pairs:
#   • Key: 10-char string → Value: 4-byte integer
#   • Key: 10-char string → Value: 30-char string  
#   • Key: 10-char string → Value: double/float
# • **{S15:{S5:I2,S5:S10}}** - Nested map:
#   • Key: 15-char string → Value: another map containing:
#     • Key: 5-char string → Value: 2-byte integer
#     • Key: 5-char string → Value: 10-char string

# ### **Complex Data:**
# • **B512** - 512 bytes of binary data
# • **{S20:B50}** - Map with:
#   • Key: 20-char string → Value: 50 bytes of binary data
