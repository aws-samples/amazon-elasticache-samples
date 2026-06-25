from valkey.commands.search.query import Query
from valkey.commands.search.field import VectorField
import valkey
import numpy as np
from langchain_community.document_loaders import WebBaseLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_aws import BedrockEmbeddings

VALKEY_ENDPOINT = ""
VALKEY_CLUSTER_MODE = False
VALKEY_PORT = 6379

WEBSITE_URL = "https://aws.amazon.com/elasticache/faqs/"
# the keyword that is used to query the website content
QUERY_STR = "Which engines does ElastiCache support?"

BEDROCK_MODEL_ID = "amazon.titan-embed-text-v2:0"
BEDROCK_REGION = "us-east-1"


# Step 1. Load website content and split document

loader = WebBaseLoader(web_path=WEBSITE_URL)
pages = loader.load()
text_splitter = RecursiveCharacterTextSplitter(
    separators=["\n\n", "\n", ".", " "],
    chunk_size=1000,
    chunk_overlap=200)
chunks = text_splitter.split_documents(pages)

print(f"Loaded and splitted content into {len(chunks)} chunks")


# Step 2. Create a vector index


VECTOR_DIMENSIONS = 1024
INDEX_NAME = 'vectorIndex'

if VALKEY_CLUSTER_MODE:
    client = valkey.ValkeyCluster(
        host=VALKEY_ENDPOINT,
        port=VALKEY_PORT,
        decode_responses=True,
    )
else:
    client = valkey.Valkey(
        host=VALKEY_ENDPOINT,
        port=VALKEY_PORT,
        decode_responses=True,
    )

try:
    client.ft(index_name=INDEX_NAME).info()
    print(f'Index "{INDEX_NAME}" already exists')
except valkey.ResponseError as e:
    if 'not found' not in str(e):
        raise e
    print(f'Creating index "{INDEX_NAME}"')
    client.ft(index_name=INDEX_NAME).create_index([
        VectorField(
            "embed",
            "HNSW",
            {
                "TYPE": "FLOAT32",
                "DIM": VECTOR_DIMENSIONS,
                "DISTANCE_METRIC": "COSINE",
            })
    ])



# Step 3. Store the chunks and embedding details into Valkey


# Requires requesting quota access
# https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html
embedding = BedrockEmbeddings(
    model_id=BEDROCK_MODEL_ID,
    region_name=BEDROCK_REGION,
)


def to_bytes(x): return np.array(x, dtype=np.float32).tobytes()


# Save embedding and metadata using hset into your ElastiCache cluster
for i, chunk in enumerate(chunks):
    content = chunk.page_content
    vector_embedding = to_bytes(embedding.embed_documents([content]))
    client.hset(f'chunkId:{i}', mapping={
                'embed': vector_embedding,
                'text': content,
                })


# Step 4. Search the vector space


# Query vector data
query = (
    Query("*=>[KNN 3 @embed $vec as score]")
    .return_fields("id", "score")
    .dialect(2)
)

# Find K similar document chunks
query_params = {
    "vec": to_bytes(embedding.embed_documents([QUERY_STR]))
}

results = client.ft(index_name=INDEX_NAME).search(query, query_params).docs

print(f'\nGot {len(results)} results:')
for idx, item in enumerate(results):
    print(f'>>> Result #{idx}: {item}')

if len(results) > 0:
    print(f'\nShowing the raw chunk for "{results[0].id}":')
    print(client.hget(results[0].id, 'text'))
