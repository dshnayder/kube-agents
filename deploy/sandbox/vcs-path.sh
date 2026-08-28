# Give the credential-free git the name `git`, but only under the version
# control abstraction.
#
# Sorted after credential-proxy-path.sh so this prepend wins. That ordering is
# the whole file: with CREDENTIAL_PROXY_VCS on, a bare `git` typed by the model
# reaches /opt/vcs/bin/git -- the local binary, no token, no wire protocol --
# instead of the shim that runs git inside the broker.
#
# This is half the delivery, and the half Hermes never reads. profile.d runs for
# a login shell -- `kubectl exec -- bash -l` -- and `ssh sandbox git log` is not
# one; its whole environment is the SetEnv line the entrypoint generates, which
# makes the same decision there. The first build of the abstraction had only
# this file and shipped an install where both names still resolved to the shim.
#
# It is off by default because the two modes want opposite answers. Without the
# abstraction there is no local clone to read, so `git` has to mean the shim or
# it means nothing. With it, the clone is right here and sending the command to
# the broker hands the credentialed process a .git/config the sandbox wrote,
# which is the exposure the abstraction exists to remove. Measured on a live
# install before this file existed: given the version-control skill and a shim
# still owning the name, an agent answered 8 of 60 read probes through the shim
# alone and issued 4 credentialed clones through it.
#
# gcloud and kubectl keep resolving to the shim: no credential-free equivalent
# of them exists and there is nothing local for them to read. `gh` is handled
# somewhere else and in the other direction -- the entrypoint deletes the shim's
# `gh` outright when the abstraction is armed, because a forge CLI on PATH is
# the route out of a forge-neutral abstraction and the model took it. Nothing
# here needs to shadow a name that no longer exists.
#
# The guard is the directory rather than the git inside it: prepending an empty
# directory costs nothing, and gating on the binary would let a later image that
# stages the local git elsewhere stop prepending without saying so.
if [ "${CREDENTIAL_PROXY_VCS:-0}" = "1" ] && [ -d /opt/vcs/bin ]; then
    export PATH="/opt/vcs/bin:${PATH}"
fi
