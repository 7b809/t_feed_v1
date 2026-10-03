import upstox_client
from upstox_client.rest import ApiException

configuration = upstox_client.Configuration()
configuration.access_token = 'eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI3QUFIUEoiLCJqdGkiOiI2YWMxMjczM2UyY2QyMjZmMjNkZDNhMWMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzkxMDQzMzc5LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3OTEwNjQ4MDB9.BjGmxJ2rYqLZhF4URr2_tw51r6ke1Dg7W_44rr0QdZg'
api_version = '2.0'

api_instance = upstox_client.UserApi(upstox_client.ApiClient(configuration))

try:
    # Get User Fund And Margin
    api_response = api_instance.get_profile(api_version)
    print(api_response)
except ApiException as e:
    print("Exception when calling UserApi->get_user_fund_margin: %s\n" % e)


