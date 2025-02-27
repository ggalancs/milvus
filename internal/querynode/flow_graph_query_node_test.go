// Licensed to the LF AI & Data foundation under one
// or more contributor license agreements. See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership. The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License. You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package querynode

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"

	"github.com/milvus-io/milvus/internal/proto/internalpb"
)

func TestQueryNodeFlowGraph_consumerFlowGraph(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	tSafe := newTSafeReplica(ctx)

	streamingReplica, err := genSimpleReplica()
	assert.NoError(t, err)

	fac, err := genFactory()
	assert.NoError(t, err)

	fg := newQueryNodeFlowGraph(ctx,
		loadTypeCollection,
		defaultCollectionID,
		defaultPartitionID,
		streamingReplica,
		tSafe,
		defaultVChannel,
		fac)

	err = fg.consumerFlowGraph(defaultVChannel, defaultSubName)
	assert.NoError(t, err)

	fg.close()
}

func TestQueryNodeFlowGraph_seekQueryNodeFlowGraph(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	streamingReplica, err := genSimpleReplica()
	assert.NoError(t, err)

	fac, err := genFactory()
	assert.NoError(t, err)

	tSafe := newTSafeReplica(ctx)

	fg := newQueryNodeFlowGraph(ctx,
		loadTypeCollection,
		defaultCollectionID,
		defaultPartitionID,
		streamingReplica,
		tSafe,
		defaultVChannel,
		fac)

	position := &internalpb.MsgPosition{
		ChannelName: defaultVChannel,
		MsgID:       []byte{},
		MsgGroup:    defaultSubName,
		Timestamp:   0,
	}
	err = fg.seekQueryNodeFlowGraph(position)
	assert.Error(t, err)

	fg.close()
}
