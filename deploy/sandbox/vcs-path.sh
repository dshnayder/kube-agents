# Give the credential-free git the name `git`.
#
# Sorted after credential-proxy-path.sh so this prepend wins. That ordering is
# the whole file: a bare `git` typed by the model reaches /opt/vcs/bin/git --
# the local binary, no token, no wire protocol -- instead of the shim that runs
# git inside the broker.
#
# Which is the right answer here because there is always a local clone to read.
# The version-control skill puts one on disk before anything else happens, and
# sending a command about it to the broker would hand the credentialed process a
# `.git/config` the sandbox wrote, which is the exposure the abstraction exists
# to remove. Measured on a live install before this file existed: given the
# version-control skill and a shim still owning the name, an agent answered 8 of
# 60 read probes through the shim alone and issued 4 credentialed clones through
# it.
#
# This is half the delivery, and the half Hermes never reads. profile.d runs for
# a login shell -- `kubectl exec -- bash -l` and the smoke test -- and `ssh
# sandbox git log` is not one; its whole environment is the SetEnv line the
# entrypoint generates, which makes the same decision there. An early build
# relied on this file alone and shipped an install where `git` still resolved to
# the shim over ssh.
#
# gcloud and kubectl keep resolving to the shim: no credential-free equivalent
# of them exists and there is nothing local for them to read. `gh` is not on
# PATH under any name -- deploy/sandbox/Dockerfile says why -- so nothing here
# needs to shadow it.
#
# The guard is the directory rather than the git inside it: prepending an empty
# directory costs nothing, while gating on the binary would let a later image
# that stages the local git elsewhere stop prepending without saying so.
if [ -d /opt/vcs/bin ]; then
    export PATH="/opt/vcs/bin:${PATH}"
fi
