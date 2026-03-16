import json
import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('movie_ticket_office')

def lambda_handler(event, context):
    name = event.get('name', None)
    response = table.scan(
        FilterExpression=Attr('name').eq(name)
    )
    results = []
    if "Items" in response:
        for item in response["Items"]:
            result = {}
            result["movie"] = item["movie"]
            result["showtime"] = item["time"]
            result["userId"] = item["name"]
            result["id"] = item["id"]
            results.append(result)

    return {
        'statusCode': 200,
        'body': json.dumps(results)
    }
