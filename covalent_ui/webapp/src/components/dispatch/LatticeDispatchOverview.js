/**
 * Copyright 2021 Agnostiq Inc.
 *
 * This file is part of Covalent.
 *
 * Licensed under the GNU Affero General Public License 3.0 (the "License").
 * A copy of the License may be obtained with this software package or at
 *
 *      https://www.gnu.org/licenses/agpl-3.0.en.html
 *
 * Use of this file is prohibited except in compliance with the License. Any
 * modifications or derivative works of this file must retain this copyright
 * notice, and modified files must contain a notice indicating that they have
 * been altered from the originals.
 *
 * Covalent is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE. See the License for more details.
 *
 * Relief from the License may be granted by purchasing a commercial license.
 */
import { Divider, Paper, Tooltip, Typography } from '@mui/material'
import { useSelector,useDispatch } from 'react-redux'
import React, { useEffect } from 'react'
import { formatDate, truncateMiddle } from '../../utils/misc'
import CopyButton from '../common/CopyButton'
import Runtime from '../dispatches/Runtime'
import SyntaxHighlighter from '../common/SyntaxHighlighter'
import Heading from '../common/Heading'
import InputSection from '../common/InputSection'
import ExecutorSection from '../common/ExecutorSection'
import { latticeResults,latticeFunctionString,latticeInput,
  latticeExecutorDetail} from '../../redux/latticeSlice'

const LatticeDispatchOverview = ({dispatchId,latDetails }) => {
  const result = latDetails
  const dispatch = useDispatch()
  const drawerInput = useSelector((state) => state.latticeResults.latticeInput)
  const drawerResult = useSelector((state) => state.latticeResults.latticeResult)
  const drawerFunctionString = useSelector((state) => state.latticeResults.latticeFunctionString)
  const drawerExecutorDetail = useSelector((state) => state.latticeResults.latticeExecutorDetail)

  useEffect(() => {
    dispatch(latticeResults({ dispatchId , params : 'result'}))
    dispatch(latticeFunctionString({dispatchId,params:'function_string'}))
    dispatch(latticeInput({dispatchId,params:'inputs'}))
    dispatch(latticeExecutorDetail({dispatchId,params:'executor_details'}))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const hasStarted = !!result.started_at
  const hasEnded = !!result.ended_at

  return (
    <>
      {/* Description */}
        {result.lattice !== undefined && (
          <>
            <Heading>Description</Heading>
            <Typography fontSize="body2.fontSize">
              {result.lattice.doc}
            </Typography>
          </>
        )}
      {/* Start/end times */}
      {hasStarted && (
        <>
          <Heading>Started{hasEnded ? ' - Ended' : ''}</Heading>
          <Typography fontSize="body2.fontSize">
            {formatDate(result.started_at)}
            {hasEnded && ` - ${formatDate(result.ended_at)}`}
          </Typography>
        </>
      )}

      {/* Runtime */}
      <Heading>Runtime</Heading>
      <Runtime
        sx={{ fontSize: 'body2.fontSize' }}
        startTime={result.started_at}
        endTime={result.ended_at}
      />

      {/* Directory */}
      <Heading>Directory</Heading>
      <Typography sx={{ overflowWrap: 'anywhere', fontSize: 'body2.fontSize' }}>
        <Tooltip title={result.directory} enterDelay={500}>
          <span>{truncateMiddle(result.directory, 10, 25)}</span>
        </Tooltip>
        <CopyButton
          content={result.directory}
          size="small"
          title="Copy results directory"
        />
      </Typography>

      {/* Input */}
      {Object.keys(drawerInput).length !==0 &&
      <InputSection inputs={drawerInput.data}/>
      }

      {/* Result */}
      {Object.keys(drawerResult).length !==0 &&
        <>
          <Heading>Result</Heading>
          <Paper elevation={0}>
            <SyntaxHighlighter language="python" src={drawerResult.data} />
          </Paper>
        </>
      }

      {/* Executor */}
      {Object.keys(drawerExecutorDetail).length !==0 &&
      <ExecutorSection metadata={drawerExecutorDetail} />
      }

      <Divider sx={{ my: 3 }} />

      {/* Source */}

      <Heading />
      {Object.keys(drawerFunctionString).length !==0 &&
      <Paper elevation={0}>
        <SyntaxHighlighter src={drawerFunctionString.data} />
      </Paper>
      }
    </>
  )
}

export default LatticeDispatchOverview
