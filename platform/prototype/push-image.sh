#!/usr/bin/env bash
# Builds the match and bot images and pushes them to ECR. Prints the tag to pass to run_batch.py.
# Needs AWS credentials for the account the images go to.
#
#   platform/prototype/push-image.sh
#       the toy games: the controller is this repository's mock controller
#   platform/prototype/push-image.sh --controller-image REF [--controller-path PATH]
#       a game's own controller image, with the controller binary at PATH (default /app/controller)
#
# The match image records the controller image's GAME_VERSION with every result. A commit hash
# there marks the real game, and its scores are then broken down (SCORE_FORMAT=win-points, see
# match_task.py). The bot image holds the sample bot; other bots plug in with run_batch.py
# --bot-image.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." # platform/, the build context

usage() { sed -n '5,8p' "${BASH_SOURCE[0]}" >&2; exit 2; }
controller_image="" controller_path=/app/controller
while (($#)); do
  case $1 in
    --controller-image) controller_image=${2:?}; shift 2 ;;
    --controller-path) controller_path=${2:?}; shift 2 ;;
    *) usage ;;
  esac
done

[[ -z "$(git status --porcelain)" ]] || { echo "Commit first: the tags name the commit." >&2; exit 1; }
region=${AWS_REGION:-eu-central-1}
account=$(aws sts get-caller-identity --query Account --output text)
registry="$account.dkr.ecr.$region.amazonaws.com"
commit=$(git rev-parse --short=12 HEAD)
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"

if [[ -z "$controller_image" ]]; then
  controller_image="dsim/controller:$commit"
  controller_path=/mock-controller
  docker build -f Dockerfile --platform linux/amd64 --target controller -t "$controller_image" .
else
  docker pull --platform linux/amd64 "$controller_image"
fi
game_version=$(docker image inspect "$controller_image" \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^GAME_VERSION=//p')
game_version=${game_version:-unknown}
score_format=plain
[[ "$game_version" =~ ^[0-9a-f]{12}$ ]] && score_format=win-points

# ECR tags are immutable: the match image's tag names both this commit and the game.
match_tag="$commit-$(tr -c 'A-Za-z0-9_.\n-' '-' <<<"$game_version")"
match_image="$registry/dsim/match:$match_tag"
bot_image="$registry/dsim/match-bot:$commit"

docker build -f prototype/Dockerfile --platform linux/amd64 --target match \
  --build-arg CONTROLLER_IMAGE="$controller_image" --build-arg CONTROLLER_PATH="$controller_path" \
  --build-arg GAME_VERSION="$game_version" --build-arg SCORE_FORMAT="$score_format" \
  --build-arg IMAGE_REVISION="$commit" -t "$match_image" .
docker build -f prototype/Dockerfile --platform linux/amd64 --target bot -t "$bot_image" .

push() { # an image whose tag already exists is the same build: skip it
  local repo=${1%%:*} tag=${1##*:}
  if aws ecr describe-images --region "$region" --repository-name "${repo#*/}" \
    --image-ids imageTag="$tag" >/dev/null 2>&1; then
    echo "$1 exists already"
  else
    docker push "$1"
  fi
}
push "$match_image"
push "$bot_image"
echo "Pushed. Use: --image-tag $match_tag (game $game_version, scores $score_format)"
