# Control-plane UI, DEVELOPMENT server. Used only by the local
# docker-compose.yml; the published image is Dockerfile.prod (nginx).
#
# ECR Public mirror of the Docker official image (not Docker Hub), pinned by
# digest so a rebuild cannot pick up different bytes under the same tag.
FROM public.ecr.aws/docker/library/node:18-alpine@sha256:8d6421d663b4c28fd3ebc498332f249011d118945588d0a35cb9bc4b8ca09d9e
WORKDIR /app
# Runs as the image's unprivileged `node` user, not root.
RUN chown node:node /app
USER node
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --chown=node:node . .
EXPOSE 3000
CMD ["npm", "start"]
