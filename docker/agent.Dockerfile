# The agent's machine: an ordinary Linux box with common tools and opencode.
# Built with the sandbox's root CA in the context, so evil-captcha.org has valid HTTPS.
FROM node:22-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget w3m jq git ripgrep python3 python3-requests \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @opencode/cli@2.0.20 && npm cache clean --force
COPY root-ca.crt /usr/local/share/ca-certificates/root-ca.crt
RUN update-ca-certificates
RUN usermod -l user -d /home/user -m node && groupmod -n user node
ENV OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1
USER user
WORKDIR /home/user
CMD ["sleep", "infinity"]
