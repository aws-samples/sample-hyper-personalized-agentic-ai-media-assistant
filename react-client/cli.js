#!/usr/bin/env node

const { CloudFormationClient, DescribeStacksCommand } = require('@aws-sdk/client-cloudformation');
const { program } = require('commander');
const fs = require('fs').promises;
const path = require('path');

// Configuration
const getDefaultRegion = () => {
    return process.env.AWS_DEFAULT_REGION || 
           'us-east-1';
};

class CDKOutputRetriever {
    constructor(region, stack) {
        this.cfClient = new CloudFormationClient({ region });
        this.stackName = stack;
    }

    /**
     * Writes .env file with stack output values
     * Maps stack outputs to React environment variables
     */
    async writeEnvFile(envFilePath = '.env') {
        try {
            const outputs = await this.getAllOutputs();
            
            // Extract required values from stack outputs
            const userPoolId = outputs.UserPoolId?.value;
            const userPoolClientId = outputs.UserPoolClientId?.value;
            const cloudFrontDistributionDomainName = outputs.CloudFrontDistributionDomainName?.value;
            const regionName = outputs.RegionName?.value;
            
            
            // Validate required outputs exist
            const missingOutputs = [];
            if (!userPoolId) missingOutputs.push('UserPoolId');
            if (!userPoolClientId) missingOutputs.push('UserPoolClientId');
            if (!cloudFrontDistributionDomainName) missingOutputs.push('CloudFrontDistributionDomainName');
            if (!regionName) missingOutputs.push('RegionName');
            
            if (missingOutputs.length > 0) {
                throw new Error(`Missing required stack outputs: ${missingOutputs.join(', ')}`);
            }
            
            // Create .env content
            const envContent = [
                '# Generated automatically from CDK stack outputs',
                `# Stack: ${this.stackName}`,
                `# Generated on: ${new Date().toISOString()}`,
                '',
                `REACT_APP_COGNITO_REGION=${regionName}`,
                `REACT_APP_COGNITO_USER_POOL_ID=${userPoolId}`,
                `REACT_APP_COGNITO_USER_POOL_CLIENT_ID=${userPoolClientId}`,
                `REACT_APP_WEBSOCKET_URL=wss://${cloudFrontDistributionDomainName}`,
                ''
            ].join('\n');
            
            // Write to file
            await fs.writeFile(envFilePath, envContent, 'utf8');
            
            return {
                success: true,
                filePath: path.resolve(envFilePath),
                values: {
                    REACT_APP_COGNITO_REGION: regionName,
                    REACT_APP_COGNITO_USER_POOL_ID: userPoolId,
                    REACT_APP_COGNITO_USER_POOL_CLIENT_ID: userPoolClientId,
                    REACT_APP_WEBSOCKET_URL: `wss://${cloudFrontDistributionDomainName}`
                }
            };
            
        } catch (error) {
            throw new Error(`Failed to write .env file: ${error.message}`);
        }
    }

    async getStackOutputs() {
        try {
            const command = new DescribeStacksCommand({
                StackName: this.stackName
            });
            
            const response = await this.cfClient.send(command);
            const stack = response.Stacks[0];
            
            if (!stack) {
                throw new Error(`Stack ${this.stackName} not found`);
            }

            return stack.Outputs || [];
        } catch (error) {
            throw new Error(`Failed to retrieve stack outputs: ${error.message}`);
        }
    }

    async getS3BucketName() {
        const outputs = await this.getStackOutputs();
        const bucketOutput = outputs.find(output => 
            output.OutputKey === 'WebsiteBucketName'
        );
        
        if (!bucketOutput) {
            throw new Error('WebsiteBucketName output not found in stack');
        }
        
        return bucketOutput.OutputValue;
    }

    async getAllOutputs() {
        const outputs = await this.getStackOutputs();
        const outputMap = {};
        
        outputs.forEach(output => {
            outputMap[output.OutputKey] = {
                value: output.OutputValue,
                description: output.Description || '',
                exportName: output.ExportName || ''
            };
        });
        
        return outputMap;
    }
}

// CLI Commands
program
    .name('cdk-outputs-cli')
    .description('CLI tool to retrieve CDK stack outputs')
    .version('1.0.0');

program
    .command('bucket')
    .description('Get the S3 bucket name from CDK outputs')
    .option('-r, --region <region>', `AWS region (default: ${getDefaultRegion()})`, getDefaultRegion())
    .option('-j, --json', 'Output as JSON')
    .option('-s, --stack', 'Cloudformation Stack Name', "NovaSonicSolutionBackendStack")
    .action(async (options) => {
        try {
            const retriever = new CDKOutputRetriever(options.region, options.stack);
            const bucketName = await retriever.getS3BucketName();
            
            if (options.json) {
                console.log(JSON.stringify({ bucketName }, null, 2));
            } else {
                console.log(`${bucketName}`);
            }
        } catch (error) {
            console.error(`Error: ${error.message}`);
            process.exit(1);
        }
    });

program
    .command('outputs')
    .description('Get all CDK stack outputs')
    .option('-r, --region <region>', `AWS region (default: ${getDefaultRegion()})`, getDefaultRegion())
    .option('-j, --json', 'Output as JSON')
    .option('-k, --key <key>', 'Get specific output by key')
    .option('-s, --stack', 'Cloudformation Stack Name', "NovaSonicSolutionBackendStack")
    .action(async (options) => {
        try {
            const retriever = new CDKOutputRetriever(options.region, options.stack);
            const outputs = await retriever.getAllOutputs();
            
            if (options.key) {
                const output = outputs[options.key];
                if (!output) {
                    console.error(`Output key '${options.key}' not found`);
                    process.exit(1);
                }
                
                if (options.json) {
                    console.log(JSON.stringify(output, null, 2));
                } else {
                    console.log(`${options.key}: ${output.value}`);
                    if (output.description) {
                        console.log(`Description: ${output.description}`);
                    }
                }
            } else {
                if (options.json) {
                    console.log(JSON.stringify(outputs, null, 2));
                } else {
                    console.log('\nCDK Stack Outputs:');
                    console.log('==================');
                    Object.entries(outputs).forEach(([key, output]) => {
                        console.log(`\n${key}: ${output.value}`);
                        if (output.description) {
                            console.log(`  Description: ${output.description}`);
                        }
                        if (output.exportName) {
                            console.log(`  Export Name: ${output.exportName}`);
                        }
                    });
                }
            }
        } catch (error) {
            console.error(`Error: ${error.message}`);
            process.exit(1);
        }
    });

program
    .command('info')
    .description('Show stack information')
    .option('-r, --region <region>', `AWS region (default: ${getDefaultRegion()})`, getDefaultRegion())
    .option('-s, --stack', 'Cloudformation Stack Name', "NovaSonicSolutionBackendStack")
    .action(async (options) => {
        try {
            const retriever = new CDKOutputRetriever(options.region, options.stack);
            const command = new DescribeStacksCommand({
                StackName: retriever.stackName
            });
            
            const response = await retriever.cfClient.send(command);
            const stack = response.Stacks[0];
            
            console.log('\nStack Information:');
            console.log('==================');
            console.log(`Name: ${stack.StackName}`);
            console.log(`Status: ${stack.StackStatus}`);
            console.log(`Region: ${options.region}`);
            console.log(`Creation Time: ${stack.CreationTime}`);
            if (stack.LastUpdatedTime) {
                console.log(`Last Updated: ${stack.LastUpdatedTime}`);
            }
            console.log(`Outputs Count: ${stack.Outputs?.length || 0}`);
        } catch (error) {
            console.error(`Error: ${error.message}`);
            process.exit(1);
        }
    });

program
    .command('write-env')
    .description('Write .env file with CDK stack outputs')
    .option('-r, --region <region>', `AWS region (default: ${getDefaultRegion()})`, getDefaultRegion())
    .option('-s, --stack <stack>', 'CloudFormation Stack Name', 'NovaSonicSolutionBackendStack')
    .option('-o, --output <file>', 'Output file path', '.env')
    .option('-j, --json', 'Output result as JSON')
    .action(async (options) => {
        try {
            const retriever = new CDKOutputRetriever(options.region, options.stack);
            const result = await retriever.writeEnvFile(options.output);
            
            if (options.json) {
                console.log(JSON.stringify(result, null, 2));
            } else {
                console.log(`✅ Successfully wrote .env file: ${result.filePath}`);
                console.log('\nEnvironment variables set:');
                Object.entries(result.values).forEach(([key, value]) => {
                    console.log(`  ${key}=${value}`);
                });
            }
        } catch (error) {
            console.error(`❌ Error: ${error.message}`);
            process.exit(1);
        }
    });

// Parse command line arguments
program.parse();
