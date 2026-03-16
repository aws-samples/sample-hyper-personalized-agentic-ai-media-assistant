import json
import boto3
import time
from botocore.exceptions import ClientError
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth, RequestError
import pprint
import random
from opensearchpy.helpers import bulk
import secrets

pp = pprint.PrettyPrinter(indent=2)


def interactive_sleep(seconds: int):
    """
    Support functionality to induce an artificial 'sleep' to the code in order to wait for resources to be available
    Args:
        seconds (int): number of seconds to sleep for
    """
    dots = ''
    for i in range(seconds):
        dots += '.'
        print(dots, end='\r')
        time.sleep(1) # nosemgrep


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

        return host, collection, collection_id, collection_name


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


    def create_vector_index(self, index_name_prefix: str, schema_json, with_suffix=True):
        """
        Create OpenSearch Serverless vector index. If existent, ignore
        Args:
            index_name: name of the vector index
        """
        
        # Create index
        try:
            if with_suffix:
                index_name = f"{index_name_prefix}-{self.suffix}"
            else:
                index_name = index_name_prefix
            response = self.oss_client.indices.create(index=index_name, body=schema_json)
            print('\nCreating index:')
            pp.pprint(response)

            # index creation can take up to a minute
            interactive_sleep(60)
            return index_name
        except RequestError as e:
            # you can delete the index if its already exists
            # oss_client.indices.delete(index=index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
        except Exception as e:
            print(f"error: {e}")
    
    def index(self, index_name, doc):
        response = self.oss_client.index(
            index=index_name,
            body=json.dumps(doc),
            params={"timeout": 60},
        )
        return response
    
    def bulk_index(self, docs):
        success, _ = bulk(self.oss_client, docs)

    def search(self, query, index):
        response = self.oss_client.search(body=query, index=index)
        return response
    