FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# bundle/ = the what-if sources plus read-only copies of the demo's decision
# code (agent, stream processor, clips feed), staged by insights/infra/stage_whatif.py.
COPY bundle/ ./
CMD ["handler.lambda_handler"]
