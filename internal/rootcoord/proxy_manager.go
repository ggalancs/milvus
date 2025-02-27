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

package rootcoord

import (
	"context"
	"encoding/json"
	"fmt"
	"path"
	"sync"

	"github.com/milvus-io/milvus/internal/log"
	"github.com/milvus-io/milvus/internal/metrics"
	"github.com/milvus-io/milvus/internal/util/sessionutil"
	"github.com/milvus-io/milvus/internal/util/typeutil"
	"go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
	"go.uber.org/zap"
)

// proxyManager manages proxy connected to the rootcoord
type proxyManager struct {
	ctx         context.Context
	cancel      context.CancelFunc
	lock        sync.Mutex
	etcdCli     *clientv3.Client
	getSessions []func([]*sessionutil.Session)
	addSessions []func(*sessionutil.Session)
	delSessions []func(*sessionutil.Session)
}

// newProxyManager helper function to create a proxyManager
// etcdEndpoints is the address list of etcd
// fns are the custom getSessions function list
func newProxyManager(ctx context.Context, etcdEndpoints []string, fns ...func([]*sessionutil.Session)) (*proxyManager, error) {
	cli, err := clientv3.New(clientv3.Config{Endpoints: etcdEndpoints})
	if err != nil {
		return nil, err
	}
	ctx2, cancel2 := context.WithCancel(ctx)
	p := &proxyManager{
		ctx:     ctx2,
		cancel:  cancel2,
		lock:    sync.Mutex{},
		etcdCli: cli,
	}
	p.getSessions = append(p.getSessions, fns...)
	return p, nil
}

// AddSession adds functions to addSessions function list
func (p *proxyManager) AddSession(fns ...func(*sessionutil.Session)) {
	p.lock.Lock()
	defer p.lock.Unlock()
	p.addSessions = append(p.addSessions, fns...)
}

// DelSession add functions to delSessions function list
func (p *proxyManager) DelSession(fns ...func(*sessionutil.Session)) {
	p.lock.Lock()
	defer p.lock.Unlock()
	p.delSessions = append(p.delSessions, fns...)
}

// WatchProxy starts a goroutine to watch proxy session changes on etcd
func (p *proxyManager) WatchProxy() error {
	ctx, cancel := context.WithTimeout(p.ctx, RequestTimeout)
	defer cancel()

	sessions, rev, err := p.getSessionsOnEtcd(ctx)
	if err != nil {
		return err
	}
	log.Debug("succeed to get sessions on etcd", zap.Any("sessions", sessions), zap.Int64("revision", rev))
	for _, f := range p.getSessions {
		f(sessions)
	}

	eventCh := p.etcdCli.Watch(
		p.ctx,
		path.Join(Params.MetaRootPath, sessionutil.DefaultServiceRoot, typeutil.ProxyRole),
		clientv3.WithPrefix(),
		clientv3.WithCreatedNotify(),
		clientv3.WithPrevKV(),
		clientv3.WithRev(rev+1),
	)
	go p.startWatchEtcd(p.ctx, eventCh)
	return nil
}

func (p *proxyManager) startWatchEtcd(ctx context.Context, eventCh clientv3.WatchChan) {
	log.Debug("start to watch etcd")
	for {
		select {
		case <-ctx.Done():
			log.Warn("stop watching etcd loop")
			return
		case event, ok := <-eventCh:
			if !ok {
				log.Warn("stop watching etcd loop due to closed etcd event channel")
				return
			}
			if err := event.Err(); err != nil {
				log.Error("received error event from etcd watcher", zap.Error(err))
				return
			}
			for _, e := range event.Events {
				var err error
				switch e.Type {
				case mvccpb.PUT:
					err = p.handlePutEvent(e)
				case mvccpb.DELETE:
					err = p.handleDeleteEvent(e)
				}
				if err != nil {
					log.Warn("failed to handle proxy event", zap.Any("event", e), zap.Error(err))
				}
			}
		}
	}
}

func (p *proxyManager) handlePutEvent(e *clientv3.Event) error {
	session, err := p.parseSession(e.Kv.Value)
	if err != nil {
		return err
	}
	log.Debug("received proxy put event with session", zap.Any("session", session))
	for _, f := range p.addSessions {
		f(session)
	}
	metrics.RootCoordProxyLister.WithLabelValues(metricProxy(session.ServerID)).Set(1)
	return nil
}

func (p *proxyManager) handleDeleteEvent(e *clientv3.Event) error {
	session, err := p.parseSession(e.PrevKv.Value)
	if err != nil {
		return err
	}
	log.Debug("received proxy delete event with session", zap.Any("session", session))
	for _, f := range p.delSessions {
		f(session)
	}
	metrics.RootCoordProxyLister.WithLabelValues(metricProxy(session.ServerID)).Set(0)
	return nil
}

func (p *proxyManager) parseSession(value []byte) (*sessionutil.Session, error) {
	session := new(sessionutil.Session)
	err := json.Unmarshal(value, session)
	if err != nil {
		return nil, err
	}
	return session, nil
}

func (p *proxyManager) getSessionsOnEtcd(ctx context.Context) ([]*sessionutil.Session, int64, error) {
	resp, err := p.etcdCli.Get(
		ctx,
		path.Join(Params.MetaRootPath, sessionutil.DefaultServiceRoot, typeutil.ProxyRole),
		clientv3.WithPrefix(),
		clientv3.WithSort(clientv3.SortByKey, clientv3.SortAscend),
	)
	if err != nil {
		return nil, 0, fmt.Errorf("proxy manager failed to watch proxy with error %w", err)
	}

	var sessions []*sessionutil.Session
	for _, v := range resp.Kvs {
		session, err := p.parseSession(v.Value)
		if err != nil {
			log.Debug("failed to unmarshal session", zap.Error(err))
			continue
		}
		sessions = append(sessions, session)
	}

	return sessions, resp.Header.Revision, nil
}

// Stop stops the proxyManager
func (p *proxyManager) Stop() {
	p.cancel()
}

// listProxyInEtcd helper function lists proxy in etcd
func listProxyInEtcd(ctx context.Context, cli *clientv3.Client) (map[int64]*sessionutil.Session, error) {
	ctx2, cancel := context.WithTimeout(ctx, RequestTimeout)
	defer cancel()
	resp, err := cli.Get(
		ctx2,
		path.Join(Params.MetaRootPath, sessionutil.DefaultServiceRoot, typeutil.ProxyRole),
		clientv3.WithPrefix(),
		clientv3.WithSort(clientv3.SortByKey, clientv3.SortAscend),
	)
	if err != nil {
		return nil, fmt.Errorf("list proxy failed, etcd error = %w", err)
	}
	sess := make(map[int64]*sessionutil.Session)
	for _, v := range resp.Kvs {
		var s sessionutil.Session
		err := json.Unmarshal(v.Value, &s)
		if err != nil {
			log.Debug("unmarshal SvrSession failed", zap.Error(err))
			continue
		}
		sess[s.ServerID] = &s
	}
	return sess, nil
}
