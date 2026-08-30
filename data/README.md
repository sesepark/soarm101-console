# Local LeRobot datasets

`scripts/record.sh` creates one timestamped directory per recording session here. Dataset contents are
not committed to source control. Local recording uses `push_to_hub=false`; upload is a separate,
explicit step after reviewing the episodes.

Required environment variables when motion is later authorized:

```bash
SOARM_ENABLE_MOTION=1 \
SOARM_TASK="Pick up the object and place it in the tray" \
SOARM_NUM_EPISODES=10 \
./scripts/record.sh
```

