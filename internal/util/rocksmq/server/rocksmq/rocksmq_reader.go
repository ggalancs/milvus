// Copyright (C) 2019-2020 Zilliz. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance
// with the License. You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software distributed under the License
// is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
// or implied. See the License for the specific language governing permissions and limitations under the License.

package rocksmq

import (
	"context"
	"errors"
	"fmt"
	"path"
	"strconv"

	"github.com/milvus-io/milvus/internal/log"
	"github.com/tecbot/gorocksdb"
)

type rocksmqReader struct {
	store      *gorocksdb.DB
	topic      string
	readerName string

	readOpts *gorocksdb.ReadOptions
	iter     *gorocksdb.Iterator

	currentID          UniqueID
	messageIDInclusive bool
	readerMutex        chan struct{}
}

//Seek seek the rocksmq reader to the pointed position
func (rr *rocksmqReader) Seek(msgID UniqueID) { //nolint:govet
	rr.currentID = msgID
	fixChanName, _ := fixChannelName(rr.topic)
	dataKey := path.Join(fixChanName, strconv.FormatInt(msgID, 10))
	rr.iter.Seek([]byte(dataKey))
	if !rr.messageIDInclusive {
		rr.currentID++
		rr.iter.Next()
	}
}

func (rr *rocksmqReader) Next(ctx context.Context) (*ConsumerMessage, error) {
	var err error
	iter := rr.iter

	var msg *ConsumerMessage
	getMsg := func() {
		key := iter.Key()
		val := iter.Value()
		tmpKey := string(key.Data())
		var msgID UniqueID
		msgID, err = strconv.ParseInt(tmpKey[FixedChannelNameLen+1:], 10, 64)
		msg = &ConsumerMessage{
			MsgID: msgID,
		}
		origData := val.Data()
		dataLen := len(origData)
		if dataLen > 0 {
			msg.Payload = make([]byte, dataLen)
			copy(msg.Payload, origData)
		}
		val.Free()
		iter.Next()
		rr.currentID = msgID
	}
	if iter.Valid() {
		getMsg()
		return msg, err
	}

	select {
	case <-ctx.Done():
		log.Debug("Stop get next reader message!")
		return nil, ctx.Err()
	case _, ok := <-rr.readerMutex:
		if !ok {
			log.Warn("reader Mutex closed")
			return nil, fmt.Errorf("reader Mutex closed")
		}
		rr.iter.Close()
		rr.iter = rr.store.NewIterator(rr.readOpts)
		fixChanName, err := fixChannelName(rr.topic)
		if err != nil {
			log.Debug("RocksMQ: fixChannelName " + rr.topic + " failed")
			return nil, err
		}
		dataKey := path.Join(fixChanName, strconv.FormatInt(rr.currentID+1, 10))
		iter = rr.iter
		iter.Seek([]byte(dataKey))
		if !iter.Valid() {
			return nil, errors.New("reader iterater is still invalid after receive mutex")
		}
		getMsg()
		return msg, err
	}
}

func (rr *rocksmqReader) HasNext() bool {
	if rr.iter.Valid() {
		return true
	}

	select {
	case _, ok := <-rr.readerMutex:
		if !ok {
			return false
		}
		rr.iter.Close()
		rr.iter = rr.store.NewIterator(rr.readOpts)
		fixChanName, err := fixChannelName(rr.topic)
		if err != nil {
			log.Debug("RocksMQ: fixChannelName " + rr.topic + " failed")
			return false
		}
		dataKey := path.Join(fixChanName, strconv.FormatInt(rr.currentID+1, 10))
		rr.iter.Seek([]byte(dataKey))
		return rr.iter.Valid()
	default:
		return false
	}
}

func (rr *rocksmqReader) Close() {
	close(rr.readerMutex)
	rr.iter.Close()
	rr.readOpts.Destroy()
}
