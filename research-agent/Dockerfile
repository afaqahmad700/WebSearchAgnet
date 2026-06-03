# Portable container — works on Hugging Face Spaces (Docker SDK), Fly.io, Railway, any Docker host.
# No dependencies to install; the app is pure standard library.
FROM python:3.12-slim

WORKDIR /app
COPY research_agent.py .

# PORT triggers the app's "deployed" mode (binds 0.0.0.0, no browser auto-open).
# 7860 is Hugging Face Spaces' default port.
ENV PORT=7860
EXPOSE 7860

# Set these at runtime (host secrets), NOT here:
#   GROQ_API_KEY   your Groq key
#   APP_PASSWORD   password to gate access
CMD ["python", "research_agent.py"]
