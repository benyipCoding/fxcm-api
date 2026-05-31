# escape=`

# ForexConnect bundles Windows native libraries, so this sidecar must run in a Windows container.
FROM python:3.7-windowsservercore-ltsc2019

WORKDIR C:/app

ENV PYTHONDONTWRITEBYTECODE=1 `
    PYTHONUNBUFFERED=1 `
    PIP_NO_CACHE_DIR=1

SHELL ["powershell", "-NoLogo", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]

COPY requirements.txt ./

# The checked-in requirements file is UTF-16, so convert it before pip installs dependencies.
RUN Get-Content -Path requirements.txt -Encoding Unicode | Set-Content -Path requirements.utf8.txt -Encoding UTF8; `
    python -m pip install --no-cache-dir -r requirements.utf8.txt; `
    Remove-Item requirements.utf8.txt

COPY . .

EXPOSE 8100

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]