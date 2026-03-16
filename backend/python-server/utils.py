import base64
import numpy as np
import wave
import io
from scipy.signal import resample_poly
import time
import json
import boto3
import time
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth, RequestError
import pprint
import random
import secrets

def audio_raw_base64_to_wav(base64_str):
    # Decode base64 to raw LPCM bytes
    audio_bytes = base64.b64decode(base64_str)

    # Convert LPCM to WAV (since Streamlit supports WAV playback)
    wav_header = (
        b"RIFF" +
        (len(audio_bytes) + 36).to_bytes(4, "little") +
        b"WAVEfmt " +
        (16).to_bytes(4, "little") +
        (1).to_bytes(2, "little") +  # PCM format
        (1).to_bytes(2, "little") +  # Mono channel
        (24000).to_bytes(4, "little") +  # Sample rate (24kHz)
        (24000 * 2).to_bytes(4, "little") +  # Byte rate (SampleRate * NumChannels * BitsPerSample/8)
        (2).to_bytes(2, "little") +  # Block align (NumChannels * BitsPerSample/8)
        (16).to_bytes(2, "little") +  # Bits per sample
        b"data" +
        len(audio_bytes).to_bytes(4, "little")
    )
    # Combine WAV header with LPCM data
    wav_audio = wav_header + audio_bytes

    duration_s = 0
    with wave.open(io.BytesIO(wav_audio), 'rb') as wav_file:
        duration_s =  wav_file.getnframes()/float(wav_file.getframerate())

    return wav_audio, duration_s

def audio_wav_to_raw(audio_bytes):
    # Read the audio data from the bytes
    with wave.open(io.BytesIO(audio_bytes), 'rb') as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        n_frames = wav_file.getnframes()
        audio_data = wav_file.readframes(n_frames)
        
    # Convert to numpy array
    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    
    # Convert to mono if needed
    if channels > 1:
        audio_array = audio_array.reshape(-1, channels)
        audio_array = np.mean(audio_array, axis=1, dtype=np.int16)
    
    # Resample to 16000Hz if needed
    if sample_rate != 16000:
        # Calculate resampling parameters
        gcd = np.gcd(sample_rate, 16000)
        audio_array = resample_poly(audio_array, 16000 // gcd, sample_rate // gcd)
        audio_array = audio_array.astype(np.int16)
    
    # Create new WAV file with 16kHz
    output_buffer = io.BytesIO()
    with wave.open(output_buffer, 'wb') as out_wav:
        out_wav.setnchannels(1)  # mono
        out_wav.setsampwidth(2)  # 16-bit
        out_wav.setframerate(16000)  # 16kHz
        out_wav.writeframes(audio_array.tobytes())
    
    return output_buffer.getvalue()

class OpenSearchHelper:
    """
    Support class that allows for:
        - creation (or retrieval) of a OpenSearch Server Collection
          (including OSS, IAM roles and Permissions and S3 bucket)
    """

    def __init__(self, suffix=None, host=None):
        """
        Class initializer
        """
        boto3_session = boto3.session.Session()
        self.region_name = boto3_session.region_name
        self.iam_client = boto3_session.client('iam',region_name=self.region_name)
        self.account_number = boto3_session.client('sts',region_name=self.region_name).get_caller_identity().get('Account')
        self.suffix = suffix if suffix else secrets.randbelow(700) + 200
        self.identity = boto3_session.client('sts',region_name=self.region_name).get_caller_identity()['Arn']
        self.aoss_client = boto3_session.client('opensearchserverless',region_name=self.region_name)
        self.s3_client = boto3_session.client('s3',region_name=self.region_name)
        credentials = boto3.Session().get_credentials()
        self.awsauth = AWSV4SignerAuth(credentials, self.region_name, 'aoss')
        if host:
            self.oss_client = OpenSearch(
            hosts=[{'host': host, 'port': 443}],
            http_auth=self.awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=300
        )
        else:   
            self.oss_client = None

    def create_oss(self, collection_name_prefix):
        """
        Function used to create an OpenSearch Serverless Collection.

        """
        collection_name = f"{collection_name_prefix}-{self.suffix}"
        encryption_policy_name = f"{collection_name_prefix}-sp-{self.suffix}"
        network_policy_name = f"{collection_name_prefix}-np-{self.suffix}"
        access_policy_name = f'{collection_name_prefix}-ap-{self.suffix}'
        encryption_policy, network_policy, access_policy = self.create_policies_in_oss(
            encryption_policy_name, collection_name, network_policy_name, access_policy_name
        )
        host, collection, collection_id, collection_arn = self._create_oss(collection_name)
        # Build the OpenSearch client
        self.oss_client = OpenSearch(
            hosts=[{'host': host, 'port': 443}],
            http_auth=self.awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=300
        )

        return host, collection, collection_id


    def create_policies_in_oss(
            self, encryption_policy_name: str, vector_store_name: str, network_policy_name: str,
            access_policy_name: str
    ):
        """
        Create OpenSearch Serverless encryption, network and data access policies.
        If policies already exist, retrieve them
        Args:
            encryption_policy_name: name of the data encryption policy
            vector_store_name: name of the vector store
            network_policy_name: name of the network policy
            access_policy_name: name of the data access policy

        Returns:
            encryption_policy, network_policy, access_policy
        """
        try:
            encryption_policy = self.aoss_client.create_security_policy(
                name=encryption_policy_name,
                policy=json.dumps(
                    {
                        'Rules': [{'Resource': ['collection/' + vector_store_name],
                                   'ResourceType': 'collection'}],
                        'AWSOwnedKey': True
                    }),
                type='encryption'
            )
        except self.aoss_client.exceptions.ConflictException:
            print(f"{encryption_policy_name} already exists, retrieving it!")
            encryption_policy = self.aoss_client.get_security_policy(
                name=encryption_policy_name,
                type='encryption'
            )

        try:
            network_policy = self.aoss_client.create_security_policy(
                name=network_policy_name,
                policy=json.dumps(
                    [
                        {'Rules': [{'Resource': ['collection/' + vector_store_name],
                                    'ResourceType': 'collection'}],
                         'AllowFromPublic': True}
                    ]),
                type='network'
            )
        except self.aoss_client.exceptions.ConflictException:
            print(f"{network_policy_name} already exists, retrieving it!")
            network_policy = self.aoss_client.get_security_policy(
                name=network_policy_name,
                type='network'
            )

        try:
            access_policy = self.aoss_client.create_access_policy(
                name=access_policy_name,
                policy=json.dumps(
                    [
                        {
                            'Rules': [
                                {
                                    'Resource': ['collection/' + vector_store_name],
                                    'Permission': [
                                        'aoss:CreateCollectionItems',
                                        'aoss:DeleteCollectionItems',
                                        'aoss:UpdateCollectionItems',
                                        'aoss:DescribeCollectionItems'],
                                    'ResourceType': 'collection'
                                },
                                {
                                    'Resource': ['index/' + vector_store_name + '/*'],
                                    'Permission': [
                                        'aoss:CreateIndex',
                                        'aoss:DeleteIndex',
                                        'aoss:UpdateIndex',
                                        'aoss:DescribeIndex',
                                        'aoss:ReadDocument',
                                        'aoss:WriteDocument'],
                                    'ResourceType': 'index'
                                }],
                            'Principal': [self.identity],
                            'Description': 'Easy data policy'}
                    ]),
                type='data'
            )
        except self.aoss_client.exceptions.ConflictException:
            print(f"{access_policy_name} already exists, retrieving it!")
            access_policy = self.aoss_client.get_access_policy(
                name=access_policy_name,
                type='data'
            )
        return encryption_policy, network_policy, access_policy

    def _create_oss(self, collection_name: str):
        """
        Create OpenSearch Serverless Collection. If already existent, retrieve
        Args:
            collection_name: name of the OSS collection
        """
        try:
            collection = self.aoss_client.create_collection(
                name=collection_name, type='VECTORSEARCH'
            )
            collection_id = collection['createCollectionDetail']['id']
            collection_arn = collection['createCollectionDetail']['arn']
        except self.aoss_client.exceptions.ConflictException:
            collection = self.aoss_client.batch_get_collection(
                names=[collection_name]
            )['collectionDetails'][0]
            pp.pprint(collection)
            collection_id = collection['id']
            collection_arn = collection['arn']
        pp.pprint(collection)

        # Get the OpenSearch serverless collection URL
        host = collection_id + '.' + self.region_name + '.aoss.amazonaws.com'
        print(host)
        # wait for collection creation
        # This can take couple of minutes to finish
        response = self.aoss_client.batch_get_collection(names=[collection_name])
        # Periodically check collection status
        while (response['collectionDetails'][0]['status']) == 'CREATING':
            print('Creating collection...')
            interactive_sleep(30)
            response = self.aoss_client.batch_get_collection(names=[collection_name])
        print('\nCollection successfully created:')
        pp.pprint(response["collectionDetails"])
        # create opensearch serverless access policy and attach it to Bedrock execution role
        return host, collection, collection_id, collection_arn


    def create_vector_index(self, index_name_prefix: str, schema_json):
        """
        Create OpenSearch Serverless vector index. If existent, ignore
        Args:
            index_name: name of the vector index
        """
        
        # Create index
        try:
            index_name = f"{index_name_prefix}-{self.suffix}"
            response = self.oss_client.indices.create(index=index_name, body=schema_json)
            print('\nCreating index:')
            return index_name
        except RequestError as e:
            # you can delete the index if its already exists
            # oss_client.indices.delete(index=index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
        except Exception as e:
            print(f"error: {e}")

    def _ensure_index_ready(self, index_name, timeout=60):
        """
        Block until index is ready, without using sleep.
        Uses OpenSearch client's built-in retry and timeout mechanisms.
        """
        try:
            # This will block and retry internally until the index is available or timeout
            # The OpenSearch client handles the waiting logic
            self.oss_client.indices.get(
                index=index_name,
                params={
                    "timeout": f"{timeout}s",
                    "master_timeout": f"{timeout}s"
                }
            )
            print(f"Index {index_name} is ready")
            return True
        except Exception as e:
            raise Exception(f"Index {index_name} is not ready after {timeout}s: {str(e)}")

    
    def index(self, index_name, doc):
        self._ensure_index_ready(index_name)
        response = self.oss_client.index(
            index=index_name,
            body=json.dumps(doc),
            params={"timeout": 60},
        )
        return response
    
    def bulk_index(self, docs):
        if docs:
            first_doc = next(iter(docs), None)
            if first_doc and '_index' in first_doc:
                self._ensure_index_ready(first_doc['_index'])
        success, _ = bulk(self.oss_client, docs)

    def search(self, query, index):
        # self._ensure_index_ready(index)
        response = self.oss_client.search(body=query, index=index)
        return response
    