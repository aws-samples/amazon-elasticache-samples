# Control-plane UI, DEVELOPMENT server. Used only by the local
# docker-compose.yml; the published image is Dockerfile.prod (nginx).
#
# ECR Public mirror of the Docker official image (not Docker Hub), pinned by
# digest so a rebuild cannot pick up different bytes under the same tag.
FROM public.ecr.aws/docker/library/node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a
WORKDIR /app
# Runs as the image's unprivileged `node` user, not root.
RUN chown node:node /app
USER node
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --chown=node:node . .
# The Vite dev server binds the container interface; compose publishes it on
# 127.0.0.1 only.
ENV VITE_HOST=0.0.0.0
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:3000/ || exit 1
CMD ["npm", "start"]
