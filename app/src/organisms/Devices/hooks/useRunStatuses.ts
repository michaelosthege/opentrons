import {
  RUN_STATUSES_TERMINAL,
  RUN_STATUS_AWAITING_RECOVERY,
  RUN_STATUS_AWAITING_RECOVERY_PAUSED,
  RUN_STATUS_IDLE,
  RUN_STATUS_PAUSED,
  RUN_STATUS_RUNNING,
} from '@opentrons/api-client'
import { useCurrentRunId } from '../../ProtocolUpload/hooks'
import { useRunStatus } from '../../RunTimeControl/hooks'

import type { RunStatus } from '@opentrons/api-client'

interface RunStatusesInfo {
  isRunStill: boolean
  isRunTerminal: boolean
  isRunIdle: boolean
  isRunRunning: boolean
}

export function useRunStatuses(): RunStatusesInfo {
  const currentRunId = useCurrentRunId()
  const runStatus = useRunStatus(currentRunId)
  const isRunIdle = runStatus === RUN_STATUS_IDLE
  const isRunRunning =
    // todo(mm, 2024-03-13): This excludes statuses like:
    // * RUN_STATUS_FINISHING
    // * RUN_STATUS_STOP_REQUESTED
    // * RUN_STATUS_BLOCKED_BY_OPEN_DOOR
    // * RUN_STATUS_AWAITING_RECOVERY_BLOCKED_BY_OPEN_DOOR
    // And it's not clear whether that's intentional.
    runStatus === RUN_STATUS_PAUSED ||
    runStatus === RUN_STATUS_RUNNING ||
    runStatus === RUN_STATUS_AWAITING_RECOVERY ||
    runStatus === RUN_STATUS_AWAITING_RECOVERY_PAUSED
  const isRunTerminal =
    runStatus != null
      ? (RUN_STATUSES_TERMINAL as RunStatus[]).includes(runStatus)
      : false
  const isRunStill = isRunTerminal || isRunIdle

  return { isRunStill, isRunTerminal, isRunIdle, isRunRunning }
}
