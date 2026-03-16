import boto3
import os
import logging
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class KnowledgeBaseDataLoader:
    """
    A class to handle loading data into Amazon Bedrock Knowledge Bases
    and syncing the data source.
    """
    
    def __init__(self, region_name: str = 'us-east-1'):
        """
        Initialize the KnowledgeBaseDataLoader.
        
        Args:
            region_name: AWS region where the knowledge base is deployed
        """
        self.region_name = region_name
        self.s3_client = boto3.client('s3', region_name=region_name)
        self.bedrock_agent_client = boto3.client('bedrock-agent', region_name=region_name)
        self.cloudformation_client = boto3.client('cloudformation', region_name=region_name)
        
        # Supported file extensions for knowledge bases
        self.supported_extensions = {'.txt', '.pdf', '.docx', '.doc', '.md', '.csv', '.html', '.json'}
    
    def upload_folder_to_s3(self, 
                           local_folder_path: str, 
                           bucket_name: str, 
                           s3_prefix: str = 'documents/') -> List[str]:
        """
        Upload all supported files from a local folder to S3.
        
        Args:
            local_folder_path: Path to the local folder containing documents
            bucket_name: Name of the S3 bucket
            s3_prefix: S3 prefix where files will be uploaded (default: 'documents/')
            
        Returns:
            List of S3 keys for uploaded files
            
        Raises:
            FileNotFoundError: If the local folder doesn't exist
            ClientError: If S3 upload fails
        """
        local_path = Path(local_folder_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local folder {local_folder_path} does not exist")
        
        if not local_path.is_dir():
            raise ValueError(f"{local_folder_path} is not a directory")
        
        uploaded_files = []
        
        # Walk through all files in the directory recursively
        for file_path in local_path.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in self.supported_extensions:
                # Create S3 key preserving the directory structure
                relative_path = file_path.relative_to(local_path)
                s3_key = f"{s3_prefix}{relative_path}"
                
                try:
                    logger.info(f"Uploading {file_path} to s3://{bucket_name}/{s3_key}")
                    
                    # Upload file to S3
                    self.s3_client.upload_file(
                        str(file_path),
                        bucket_name,
                        s3_key,
                        ExtraArgs={
                            'ContentType': self._get_content_type(file_path.suffix)
                        }
                    )
                    uploaded_files.append(s3_key)
                    logger.info(f"Successfully uploaded {s3_key}")
                    
                except ClientError as e:
                    logger.error(f"Failed to upload {file_path}: {e}")
                    raise
            else:
                if file_path.is_file():
                    logger.warning(f"Skipping unsupported file: {file_path}")
        
        logger.info(f"Successfully uploaded {len(uploaded_files)} files to S3")
        return uploaded_files
    
    def start_ingestion_job(self, 
                           knowledge_base_id: str, 
                           data_source_id: str) -> str:
        """
        Start an ingestion job to sync the data source with the knowledge base.
        
        Args:
            knowledge_base_id: ID of the Bedrock knowledge base
            data_source_id: ID of the data source
            
        Returns:
            Ingestion job ID
            
        Raises:
            ClientError: If the ingestion job fails to start
        """
        try:
            logger.info(f"Starting ingestion job for knowledge base {knowledge_base_id}")
            
            response = self.bedrock_agent_client.start_ingestion_job(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                description=f"Ingestion job started at {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            ingestion_job_id = response['ingestionJob']['ingestionJobId']
            logger.info(f"Started ingestion job with ID: {ingestion_job_id}")
            
            return ingestion_job_id
            
        except ClientError as e:
            logger.error(f"Failed to start ingestion job: {e}")
            raise
    
    def wait_for_ingestion_completion(self, 
                                    knowledge_base_id: str, 
                                    data_source_id: str, 
                                    ingestion_job_id: str,
                                    max_wait_time: int = 1800,
                                    check_interval: int = 30) -> Dict[str, Any]:
        """
        Wait for the ingestion job to complete.
        
        Args:
            knowledge_base_id: ID of the Bedrock knowledge base
            data_source_id: ID of the data source
            ingestion_job_id: ID of the ingestion job
            max_wait_time: Maximum time to wait in seconds (default: 30 minutes)
            check_interval: How often to check status in seconds (default: 30 seconds)
            
        Returns:
            Final ingestion job status
            
        Raises:
            TimeoutError: If the job doesn't complete within max_wait_time
            ClientError: If checking job status fails
        """
        start_time = time.time()
        
        while time.time() - start_time < max_wait_time:
            try:
                response = self.bedrock_agent_client.get_ingestion_job(
                    knowledgeBaseId=knowledge_base_id,
                    dataSourceId=data_source_id,
                    ingestionJobId=ingestion_job_id
                )
                
                job_status = response['ingestionJob']['status']
                logger.info(f"Ingestion job status: {job_status}")
                
                if job_status in ['COMPLETE', 'FAILED', 'STOPPED']:
                    if job_status == 'COMPLETE':
                        logger.info("Ingestion job completed successfully!")
                    else:
                        logger.error(f"Ingestion job failed with status: {job_status}")
                    
                    return response['ingestionJob']
                
                time.sleep(check_interval) # nosemgrep
                
            except ClientError as e:
                logger.error(f"Failed to check ingestion job status: {e}")
                raise
        
        raise TimeoutError(f"Ingestion job did not complete within {max_wait_time} seconds")
    
    def load_data_and_sync(self, 
                          local_folder_path: str,
                          bucket_name: str,
                          knowledge_base_id: str,
                          data_source_id: str,
                          s3_prefix: str = 'documents/',
                          wait_for_completion: bool = True) -> Dict[str, Any]:
        """
        Complete workflow: upload data from folder to S3 and sync knowledge base.
        
        Args:
            local_folder_path: Path to the local folder containing documents
            bucket_name: Name of the S3 bucket
            knowledge_base_id: ID of the Bedrock knowledge base
            data_source_id: ID of the data source
            s3_prefix: S3 prefix where files will be uploaded (default: 'documents/')
            wait_for_completion: Whether to wait for ingestion to complete
            
        Returns:
            Dictionary containing upload results and ingestion job info
        """
        results = {
            'uploaded_files': [],
            'ingestion_job_id': None,
            'ingestion_status': None
        }
        
        try:
            # Step 1: Upload files to S3
            logger.info("Starting data upload to S3...")
            uploaded_files = self.upload_folder_to_s3(
                local_folder_path, 
                bucket_name, 
                s3_prefix
            )
            results['uploaded_files'] = uploaded_files
            
            if not uploaded_files:
                logger.warning("No files were uploaded. Skipping ingestion.")
                return results
            
            # Step 2: Start ingestion job
            logger.info("Starting knowledge base ingestion...")
            ingestion_job_id = self.start_ingestion_job(
                knowledge_base_id, 
                data_source_id
            )
            results['ingestion_job_id'] = ingestion_job_id
            
            # Step 3: Wait for completion if requested
            if wait_for_completion:
                logger.info("Waiting for ingestion to complete...")
                job_status = self.wait_for_ingestion_completion(
                    knowledge_base_id,
                    data_source_id,
                    ingestion_job_id
                )
                results['ingestion_status'] = job_status
            
            logger.info("Data loading and sync completed successfully!")
            return results
            
        except Exception as e:
            logger.error(f"Error during data loading and sync: {e}")
            raise
    
    def _get_content_type(self, file_extension: str) -> str:
        """
        Get the appropriate content type for a file extension.
        
        Args:
            file_extension: File extension (e.g., '.pdf', '.txt')
            
        Returns:
            MIME content type
        """
        content_types = {
            '.txt': 'text/plain',
            '.pdf': 'application/pdf',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.doc': 'application/msword',
            '.md': 'text/markdown',
            '.csv': 'text/csv',
            '.html': 'text/html',
            '.json': 'application/json'
        }
        
        return content_types.get(file_extension.lower(), 'application/octet-stream')
    
    def list_knowledge_bases(self) -> List[Dict[str, Any]]:
        """
        List all available knowledge bases in the account.
        
        Returns:
            List of knowledge bases with their IDs and metadata
        """
        try:
            response = self.bedrock_agent_client.list_knowledge_bases()
            return response.get('knowledgeBaseSummaries', [])
        except ClientError as e:
            logger.error(f"Failed to list knowledge bases: {e}")
            raise
    
    def list_data_sources(self, knowledge_base_id: str) -> List[Dict[str, Any]]:
        """
        List all data sources for a specific knowledge base.
        
        Args:
            knowledge_base_id: ID of the knowledge base
            
        Returns:
            List of data sources with their IDs and metadata
        """
        try:
            response = self.bedrock_agent_client.list_data_sources(
                knowledgeBaseId=knowledge_base_id
            )
            return response.get('dataSourceSummaries', [])
        except ClientError as e:
            logger.error(f"Failed to list data sources: {e}")
            raise
    
    def get_stack_outputs(self, 
                         stack_name: str, 
                         output_keys: List[str] = None) -> Dict[str, str]:
        """
        Get CloudFormation stack outputs, equivalent to:
        aws cloudformation describe-stacks --stack-name STACK_NAME --query 'Stacks[0].Outputs[?OutputKey==`KEY1` || OutputKey==`KEY2`].{Key:OutputKey,Value:OutputValue}'
        
        Args:
            stack_name: Name of the CloudFormation stack
            output_keys: List of output keys to filter for. If None, returns all outputs.
            
        Returns:
            Dictionary mapping output keys to their values
            
        Raises:
            ClientError: If the stack doesn't exist or can't be accessed
        """
        try:
            logger.info(f"Getting outputs for CloudFormation stack: {stack_name}")
            
            response = self.cloudformation_client.describe_stacks(StackName=stack_name)
            
            if not response.get('Stacks'):
                raise ValueError(f"Stack {stack_name} not found")
            
            stack = response['Stacks'][0]
            outputs = stack.get('Outputs', [])
            
            result = {}
            
            for output in outputs:
                output_key = output.get('OutputKey')
                output_value = output.get('OutputValue')
                
                # If specific keys are requested, filter for them
                if output_keys is None or output_key in output_keys:
                    result[output_key] = output_value
            
            logger.info(f"Found {len(result)} matching outputs")
            return result
            
        except ClientError as e:
            logger.error(f"Failed to get stack outputs: {e}")
            raise
    
    def get_knowledge_base_stack_outputs(self, stack_name: str = 'BedrockKBStack') -> Dict[str, str]:
        """
        Get the specific outputs needed for knowledge base operations from a CloudFormation stack.
        Equivalent to: aws cloudformation describe-stacks --stack-name BedrockKBStack 
                      --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseDataBucket` || OutputKey==`KnowledgeBaseDataSourceId` || OutputKey==`KnowledgeBaseId`].OutputValue' 
                      --output json
        
        Args:
            stack_name: Name of the CloudFormation stack (default: 'BedrockKBStack')
            
        Returns:
            Dictionary with keys: bucket_name, knowledge_base_id, data_source_id
            
        Raises:
            ValueError: If required outputs are missing from the stack
        """
        required_outputs = [
            'KnowledgeBaseDataBucket',
            'KnowledgeBaseDataSourceId', 
            'KnowledgeBaseId'
        ]
        
        outputs = self.get_stack_outputs(stack_name, required_outputs)
        
        # Map CloudFormation output keys to more friendly names
        result = {}
        output_mapping = {
            'KnowledgeBaseDataBucket': 'bucket_name',
            'KnowledgeBaseId': 'knowledge_base_id',
            'KnowledgeBaseDataSourceId': 'data_source_id'
        }
        
        missing_outputs = []
        for cf_key, friendly_key in output_mapping.items():
            if cf_key in outputs:
                result[friendly_key] = outputs[cf_key]
            else:
                missing_outputs.append(cf_key)
        
        if missing_outputs:
            raise ValueError(f"Missing required outputs from stack {stack_name}: {missing_outputs}")
        
        logger.info(f"Retrieved knowledge base configuration from stack {stack_name}")
        return result


# Convenience function for simple usage
def load_folder_to_knowledge_base(local_folder_path: str,
                                 bucket_name: str,
                                 knowledge_base_id: str,
                                 data_source_id: str,
                                 region_name: str = 'us-east-1',
                                 s3_prefix: str = 'documents/',
                                 wait_for_completion: bool = True) -> Dict[str, Any]:
    """
    Convenience function to load a folder into a knowledge base.
    
    Args:
        local_folder_path: Path to the local folder containing documents
        bucket_name: Name of the S3 bucket
        knowledge_base_id: ID of the Bedrock knowledge base
        data_source_id: ID of the data source
        region_name: AWS region (default: 'us-east-1')
        s3_prefix: S3 prefix where files will be uploaded (default: 'documents/')
        wait_for_completion: Whether to wait for ingestion to complete
        
    Returns:
        Dictionary containing upload results and ingestion job info
    """
    loader = KnowledgeBaseDataLoader(region_name=region_name)
    
    return loader.load_data_and_sync(
        local_folder_path=local_folder_path,
        bucket_name=bucket_name,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
        s3_prefix=s3_prefix,
        wait_for_completion=wait_for_completion
    )


def get_cloudformation_stack_outputs(stack_name: str, 
                                   output_keys: List[str] = None,
                                   region_name: str = 'us-east-1') -> Dict[str, str]:
    """
    Convenience function to get CloudFormation stack outputs.
    Equivalent to: aws cloudformation describe-stacks --stack-name STACK_NAME
    
    Args:
        stack_name: Name of the CloudFormation stack
        output_keys: List of output keys to filter for. If None, returns all outputs.
        region_name: AWS region (default: 'us-east-1')
        
    Returns:
        Dictionary mapping output keys to their values
    """
    loader = KnowledgeBaseDataLoader(region_name=region_name)
    return loader.get_stack_outputs(stack_name, output_keys)


def get_knowledge_base_config_from_stack(stack_name: str = 'BedrockKBStack',
                                        region_name: str = 'us-east-1') -> Dict[str, str]:
    """
    Convenience function to get knowledge base configuration from CloudFormation stack.
    Equivalent to: aws cloudformation describe-stacks --stack-name BedrockKBStack 
                  --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseDataBucket` || OutputKey==`KnowledgeBaseDataSourceId` || OutputKey==`KnowledgeBaseId`].OutputValue' 
                  --output json
    
    Args:
        stack_name: Name of the CloudFormation stack (default: 'BedrockKBStack')
        region_name: AWS region (default: 'us-east-1')
        
    Returns:
        Dictionary with keys: bucket_name, knowledge_base_id, data_source_id
    """
    loader = KnowledgeBaseDataLoader(region_name=region_name)
    return loader.get_knowledge_base_stack_outputs(stack_name)


def load_folder_from_stack_config(local_folder_path: str,
                                 stack_name: str = 'BedrockKBStack',
                                 region_name: str = 'us-east-1',
                                 s3_prefix: str = 'documents/',
                                 wait_for_completion: bool = True) -> Dict[str, Any]:
    """
    Convenience function to load a folder into a knowledge base using CloudFormation stack outputs.
    This combines getting the stack configuration and loading data in one step.
    
    Args:
        local_folder_path: Path to the local folder containing documents
        stack_name: Name of the CloudFormation stack (default: 'BedrockKBStack')
        region_name: AWS region (default: 'us-east-1')
        s3_prefix: S3 prefix where files will be uploaded (default: 'documents/')
        wait_for_completion: Whether to wait for ingestion to complete
        
    Returns:
        Dictionary containing upload results and ingestion job info
    """
    # Get configuration from CloudFormation stack
    config = get_knowledge_base_config_from_stack(stack_name, region_name)
    
    # Load data using the stack configuration
    return load_folder_to_knowledge_base(
        local_folder_path=local_folder_path,
        bucket_name=config['bucket_name'],
        knowledge_base_id=config['knowledge_base_id'],
        data_source_id=config['data_source_id'],
        region_name=region_name,
        s3_prefix=s3_prefix,
        wait_for_completion=wait_for_completion
    )


if __name__ == "__main__":
    # Example usage
    import argparse
    
    parser = argparse.ArgumentParser(description='Load data into Bedrock Knowledge Base')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Load data command
    load_parser = subparsers.add_parser('load', help='Load data into knowledge base')
    load_parser.add_argument('--folder', required=True, help='Local folder path containing documents')
    load_parser.add_argument('--bucket', help='S3 bucket name (required if not using --stack)')
    load_parser.add_argument('--kb-id', help='Knowledge Base ID (required if not using --stack)')
    load_parser.add_argument('--ds-id', help='Data Source ID (required if not using --stack)')
    load_parser.add_argument('--stack', help='CloudFormation stack name to get configuration from')
    load_parser.add_argument('--region', default='us-east-1', help='AWS region')
    load_parser.add_argument('--prefix', default='documents/', help='S3 prefix for uploads')
    load_parser.add_argument('--no-wait', action='store_true', help='Do not wait for ingestion completion')
    
    # Get stack outputs command
    stack_parser = subparsers.add_parser('get-stack-outputs', help='Get CloudFormation stack outputs')
    stack_parser.add_argument('--stack', required=True, help='CloudFormation stack name')
    stack_parser.add_argument('--keys', nargs='*', help='Specific output keys to retrieve (default: all)')
    stack_parser.add_argument('--region', default='us-east-1', help='AWS region')
    
    # Get knowledge base config command
    kb_config_parser = subparsers.add_parser('get-kb-config', help='Get knowledge base configuration from CloudFormation stack')
    kb_config_parser.add_argument('--stack', default='BedrockKBStack', help='CloudFormation stack name')
    kb_config_parser.add_argument('--region', default='us-east-1', help='AWS region')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        exit(1)
    
    try:
        if args.command == 'load':
            if args.stack:
                # Load using CloudFormation stack configuration
                results = load_folder_from_stack_config(
                    local_folder_path=args.folder,
                    stack_name=args.stack,
                    region_name=args.region,
                    s3_prefix=args.prefix,
                    wait_for_completion=not args.no_wait
                )
            else:
                # Load using explicit parameters
                if not all([args.bucket, args.kb_id, args.ds_id]):
                    print("Error: --bucket, --kb-id, and --ds-id are required when not using --stack")
                    exit(1)
                
                results = load_folder_to_knowledge_base(
                    local_folder_path=args.folder,
                    bucket_name=args.bucket,
                    knowledge_base_id=args.kb_id,
                    data_source_id=args.ds_id,
                    region_name=args.region,
                    s3_prefix=args.prefix,
                    wait_for_completion=not args.no_wait
                )
            
            print(f"Successfully processed {len(results['uploaded_files'])} files")
            print(f"Ingestion job ID: {results['ingestion_job_id']}")
            
            if results['ingestion_status']:
                print(f"Final ingestion status: {results['ingestion_status']['status']}")
                
        elif args.command == 'get-stack-outputs':
            outputs = get_cloudformation_stack_outputs(
                stack_name=args.stack,
                output_keys=args.keys,
                region_name=args.region
            )
            
            import json
            print(json.dumps(outputs, indent=2))
            
        elif args.command == 'get-kb-config':
            config = get_knowledge_base_config_from_stack(
                stack_name=args.stack,
                region_name=args.region
            )
            
            import json
            print(json.dumps(config, indent=2))
            
    except Exception as e:
        logger.error(f"Command failed: {e}")
        exit(1)

