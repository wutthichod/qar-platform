"""Silver assets: the sweep.

The event path handles normal arrivals -- S3 fires, a job runs, no
orchestrator involved. This sweep exists for what the event path
structurally cannot catch:

  - a FAP that was not loaded when the file arrived
  - a decoder bug fixed after the fact
  - a FAP correction invalidating flights that decoded successfully
  - an S3 event that was throttled or lost, leaving no trace at all

None of these produce a new S3 event when the underlying problem is fixed.
The file arrived last Tuesday; S3 will not announce it again.

The query is the whole design:

    SELECT flight_id FROM registry
    WHERE status = 'failed' AND reason_is_retryable
       OR fap_version != :current_fap
       OR decoder_version != :current_decoder
       OR (status = 'pending' AND received_at < now() - interval '1 hour')

Submits to the low-priority backfill queue, so a fleet-wide re-decode never
delays a flight that landed an hour ago.
"""

assets: list = []
