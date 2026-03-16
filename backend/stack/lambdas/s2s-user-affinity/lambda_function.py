import json
import boto3

dynamodb = boto3.client('dynamodb')
table_name = 'user_affinity'
    
def lambda_handler(event, context):
    print(event)
    user_id = event.get('userId', None)
    print(f"user id is: {user_id}")
    key = { "id" : { "S" : user_id }}

    response = dynamodb.get_item(
        TableName=table_name,
        Key=key,
        ProjectionExpression="id, genres"
    )

    user_genres = ""
    user_affinity = []
    if 'Item' in response:
        result = {}
        result['userId'] = response['Item']['id']['S']
        result['genres'] = response['Item']['genres']['S']
        user_affinity.append(result)
        
    return {
        'statusCode': 200,
        'body': json.dumps(user_affinity)
    }