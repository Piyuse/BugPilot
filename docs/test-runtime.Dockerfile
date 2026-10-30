# Build this FROM THE TARGET REPOSITORY as the Docker build context.
# Adapt requirements paths and installation for that repository.
FROM python:3.12-slim
WORKDIR /opt/runtime
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest
# BugPilot copies source to /work and supplies the configured command.
WORKDIR /work
