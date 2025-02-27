from time import time, sleep

import pytest
from pymilvus.grpc_gen.common_pb2 import SegmentState

from base.client_base import TestcaseBase
from common import common_func as cf
from common import common_type as ct
from common.common_type import CaseLabel, CheckTasks
from utils.util_log import test_log as log

prefix = "compact"
tmp_nb = 100


# @pytest.mark.skip(reason="Ci failed")
class TestCompactionParams(TestcaseBase):

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_without_connection(self):
        """
        target: test compact without connection
        method: compact after remove connection
        expected: raise exception
        """
        # init collection with tmp_nb default data
        collection_w = self.init_collection_general(prefix, nb=tmp_nb, insert_data=True)[0]

        # remove connection and delete
        self.connection_wrap.remove_connection(ct.default_alias)
        res_list, _ = self.connection_wrap.list_connections()
        assert ct.default_alias not in res_list
        error = {ct.err_code: 0, ct.err_msg: "should create connect first"}
        collection_w.compact(check_task=CheckTasks.err_res, check_items=error)

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_twice(self):
        """
        target: test compact twice
        method: 1.create with shard_num=1
                2.insert and flush twice (two segments)
                3.compact
                4.insert new data
                5.compact
        expected: Merge into one segment
        """
        # init collection with one shard, insert into two segments
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, nb_of_segment=tmp_nb)

        # first compact two segments
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans1 = collection_w.get_compaction_plans()[0]
        target_1 = c_plans1.plans[0].target

        # insert new data
        df = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df)
        log.debug(collection_w.num_entities)

        # second compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_state()
        c_plans2 = collection_w.get_compaction_plans()[0]

        assert target_1 in c_plans2.plans[0].sources
        log.debug(c_plans2.plans[0].target)

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_partition(self):
        """
        target: test compact partition
        method: compact partition
        expected: Verify partition segments merged
        """
        # create collection with shard_num=1, and create partition
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        partition_w = self.init_partition_wrap(collection_wrap=collection_w)

        # insert flush twice
        for i in range(2):
            df = cf.gen_default_dataframe_data(tmp_nb)
            partition_w.insert(df)
            assert partition_w.num_entities == tmp_nb * (i + 1)

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans = collection_w.get_compaction_plans()[0]

        assert len(c_plans.plans) == 1
        assert len(c_plans.plans[0].sources) == 2
        target = c_plans.plans[0].target

        # verify queryNode load the compacted segments
        collection_w.load()
        segment_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        assert target == segment_info[0].segmentID

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_only_growing_segment(self):
        """
        target: test compact growing data
        method: 1.insert into multi segments without flush
                2.compact
        expected: No compaction (compact just for sealed data)
        """
        # create and insert without flush
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix))
        df = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df)

        # compact when only growing segment
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans = collection_w.get_compaction_plans()[0]
        assert len(c_plans.plans) == 0

        collection_w.load()
        segments_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        for segment_info in segments_info:
            assert segment_info.state == SegmentState.Growing

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_empty_collection(self):
        """
        target: test compact an empty collection
        method: compact an empty collection
        expected: No exception
        """
        # init collection and empty
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix))

        # compact
        collection_w.compact()
        c_plans, _ = collection_w.get_compaction_plans()
        assert len(c_plans.plans) == 0

    @pytest.mark.tags(CaseLabel.L1)
    @pytest.mark.parametrize("delete_pos", [1, tmp_nb // 2])
    def test_compact_after_delete(self, delete_pos):
        """
        target: test delete one entity and compact
        method: 1.create with shard_num=1
                2.delete one sealed entity, half entities
                2.compact
        expected: Verify compact result
        """
        # create, insert without flush
        collection_w = self.init_collection_wrap(cf.gen_unique_str(prefix))
        df = cf.gen_default_dataframe_data(tmp_nb)
        insert_res, _ = collection_w.insert(df)

        # delete single entity, flush
        single_expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys[:delete_pos]}'
        collection_w.delete(single_expr)
        assert collection_w.num_entities == tmp_nb

        # compact, get plan
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans = collection_w.get_compaction_plans()[0]

        # Delete type compaction just merge insert log and delta log of one segment
        # todo assert len(c_plans.plans[0].sources) == 1

        collection_w.load()
        collection_w.query(single_expr, check_items=CheckTasks.check_query_empty)

        res = df.iloc[-1:, :1].to_dict('records')
        collection_w.query(f'{ct.default_int64_field_name} in {insert_res.primary_keys[-1:]}',
                           check_items={'exp_res': res})

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_delete_ratio(self):
        """
        target: test delete entities reaches ratio and auto-compact
        method: 1.create with shard_num=1
                2.insert (compact load delta log, not from dmlChannel)
                3.delete 20% of nb, flush
        expected: Verify auto compaction, merge insert log and delta log
        """
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        df = cf.gen_default_dataframe_data(tmp_nb)
        insert_res, _ = collection_w.insert(df)

        # delete 20% entities
        ratio_expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys[:tmp_nb // ct.compact_delta_ratio_reciprocal]}'
        collection_w.delete(ratio_expr)
        assert collection_w.num_entities == tmp_nb

        # auto_compact
        sleep(1)
        # Delete type compaction just merge insert log and delta log of one segment
        # todo assert len(c_plans.plans[0].sources) == 1

        collection_w.load()
        collection_w.query(ratio_expr, check_items=CheckTasks.check_query_empty)

        res = df.iloc[-1:, :1].to_dict('records')
        collection_w.query(f'{ct.default_int64_field_name} in {insert_res.primary_keys[-1:]}',
                           check_items={'exp_res': res})

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_delete_less_ratio(self):
        """
        target: test delete entities less ratio and no compact
        method: 1.create collection shard_num=1
                2.insert without flush
                3.delete 10% entities and flush
        expected: Verify no compact (can't), delete successfully
        """
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        df = cf.gen_default_dataframe_data(tmp_nb)
        insert_res, _ = collection_w.insert(df)

        # delete 10% entities, ratio = 0.1
        less_ratio_reciprocal = 10
        ratio_expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys[:tmp_nb // less_ratio_reciprocal]}'
        collection_w.delete(ratio_expr)
        assert collection_w.num_entities == tmp_nb

        collection_w.load()
        collection_w.query(ratio_expr, check_task=CheckTasks.check_query_empty)

    @pytest.mark.tags(CaseLabel.L0)
    def test_compact_after_delete_all(self):
        """
        target: test delete all and compact
        method: 1.create with shard_num=1
                2.delete all sealed data
                3.compact
        expected: collection num_entities is close to 0
        """
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        df = cf.gen_default_dataframe_data()
        res, _ = collection_w.insert(df)

        expr = f'{ct.default_int64_field_name} in {res.primary_keys}'
        collection_w.delete(expr)
        assert collection_w.num_entities == ct.default_nb

        # currently no way to verify whether it is compact after delete,
        # because the merge compact plan is generate first
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()
        log.debug(collection_w.num_entities)

        collection_w.load()
        collection_w.query(expr, check_items=CheckTasks.check_query_empty)

    @pytest.mark.skip(reason="TODO")
    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_delete_max_delete_size(self):
        """
        target: test compact delta log reaches max delete size 10MiB
        method: todo
        expected: auto merge single segment
        """
        pass

    @pytest.mark.xfail(reason="Issue 12344")
    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_max_time_interval(self):
        """
        target: test auto compact with max interval 60s
        method: 1.create with shard_num=1
                2.insert flush twice (two segments)
                3.wait max_compaction_interval (60s)
        expected: Verify compaction results
        """
        # create collection shard_num=1, insert 2 segments, each with tmp_nb entities
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        collection_w.compact()

        for i in range(2):
            df = cf.gen_default_dataframe_data(tmp_nb)
            collection_w.insert(df)
            assert collection_w.num_entities == tmp_nb * (i + 1)

        sleep(61)

        # verify queryNode load the compacted segments
        collection_w.load()
        segment_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]


class TestCompactionOperation(TestcaseBase):

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_both_delete_merge(self):
        """
        target: test compact both delete and merge
        method: 1.create collection with shard_num=1
                2.insert data into two segments
                3.delete and flush (new insert)
                4.compact
                5.load and search
        expected:
        """
        collection_w = self.init_collection_wrap(cf.gen_unique_str(prefix), shards_num=1)
        ids = []
        for i in range(2):
            df = cf.gen_default_dataframe_data(tmp_nb, start=i * tmp_nb)
            insert_res, _ = collection_w.insert(df)
            assert collection_w.num_entities == (i + 1) * tmp_nb
            ids.extend(insert_res.primary_keys)

        expr = f'{ct.default_int64_field_name} in {[0, 2 * tmp_nb - 1]}'
        collection_w.delete(expr)

        collection_w.insert(cf.gen_default_dataframe_data(1, start=2 * tmp_nb))
        assert collection_w.num_entities == 2 * tmp_nb + 1

        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # search
        sleep(5)
        ids.pop(0)
        ids.pop(-1)
        collection_w.load()
        search_res, _ = collection_w.search(cf.gen_vectors(ct.default_nq, ct.default_dim),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit,
                                            check_items={"nq": ct.default_nq,
                                                         "ids": ids,
                                                         "limit": ct.default_limit})

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_after_index(self):
        """
        target: test compact after create index
        method: 1.insert data into two segments
                2.create index
                3.compact
                4.search
        expected: Verify segment info and index info
        """
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, nb_of_segment=ct.default_nb, is_dup=False)

        # create index
        collection_w.create_index(ct.default_float_vec_field_name, ct.default_index)
        log.debug(collection_w.index())

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # search
        collection_w.load()
        search_res, _ = collection_w.search(cf.gen_vectors(ct.default_nq, ct.default_dim),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit)
        assert len(search_res) == ct.default_nq
        for hits in search_res:
            assert len(hits) == ct.default_limit

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_after_binary_index(self):
        """
        target: test compact after create index
        method: 1.insert binary data into two segments
                2.create binary index
                3.compact
                4.search
        expected: Verify segment info and index info
        """
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1,
                                                 schema=cf.gen_default_binary_collection_schema())
        for i in range(2):
            df, _ = cf.gen_default_binary_dataframe_data(ct.default_nb)
            collection_w.insert(data=df)
            assert collection_w.num_entities == (i + 1) * ct.default_nb

        # create index
        collection_w.create_index(ct.default_binary_vec_field_name, ct.default_binary_index)
        log.debug(collection_w.index())

        collection_w.load()

        search_params = {"metric_type": "JACCARD", "params": {"nprobe": 10}}
        vectors = cf.gen_binary_vectors(ct.default_nq, ct.default_dim)[1]
        search_res_one, _ = collection_w.search(vectors,
                                                ct.default_binary_vec_field_name,
                                                search_params, ct.default_limit)
        assert len(search_res_one) == ct.default_nq
        for hits in search_res_one:
            assert len(hits) == ct.default_limit

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # verify index re-build and re-load
        search_params = {"metric_type": "L1", "params": {"nprobe": 10}}
        search_res_two, _ = collection_w.search(vectors,
                                                ct.default_binary_vec_field_name,
                                                search_params, ct.default_limit,
                                                check_task=CheckTasks.err_res,
                                                check_items={ct.err_code: 1,
                                                             ct.err_msg: "Metric type of field index isn't "
                                                                         "the same with search info"})

        # verify search result
        search_params = {"metric_type": "JACCARD", "params": {"nprobe": 10}}
        search_res_two, _ = collection_w.search(vectors,
                                                ct.default_binary_vec_field_name,
                                                search_params, ct.default_limit)
        assert len(search_res_two) == ct.default_nq
        for hits in search_res_two:
            assert len(hits) == ct.default_limit

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_and_index(self):
        """
        target: test compact and create index
        method: 1.insert data into two segments
                2.compact
                3.create index
                4.load and search
        expected: Verify search result and index info
        """
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, nb_of_segment=ct.default_nb, is_dup=False)

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # create index
        collection_w.create_index(ct.default_float_vec_field_name, ct.default_index)
        log.debug(collection_w.index())

        # search
        collection_w.load()
        search_res, _ = collection_w.search(cf.gen_vectors(ct.default_nq, ct.default_dim),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit)
        assert len(search_res) == ct.default_nq
        for hits in search_res:
            assert len(hits) == ct.default_limit

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_delete_and_search(self):
        """
        target: test delete and compact segment, and search
        method: 1.create collection and insert
                2.delete part entities
                3.compact
                4.load and search
        expected: Verify search result
        """
        collection_w = self.init_collection_wrap(cf.gen_unique_str(prefix), shards_num=1)
        df = cf.gen_default_dataframe_data()
        insert_res, _ = collection_w.insert(df)

        expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys[:ct.default_nb // 2]}'
        collection_w.delete(expr)
        assert collection_w.num_entities == ct.default_nb
        collection_w.compact()

        # search
        sleep(2)
        collection_w.load()
        search_res, _ = collection_w.search(cf.gen_vectors(ct.default_nq, ct.default_dim),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit,
                                            check_task=CheckTasks.check_search_results,
                                            check_items={"nq": ct.default_nq,
                                                         "ids": insert_res.primary_keys[ct.default_nb // 2:],
                                                         "limit": ct.default_limit}
                                            )

    @pytest.mark.tags(CaseLabel.L0)
    def test_compact_merge_and_search(self):
        """
        target: test compact and search
        method: 1.insert data into two segments
                2.compact
                3.load and search
        expected: Verify search result
        """
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, nb_of_segment=ct.default_nb)

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # search
        collection_w.load()
        search_res, _ = collection_w.search(cf.gen_vectors(ct.default_nq, ct.default_dim),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit)
        assert len(search_res) == ct.default_nq
        for hits in search_res:
            assert len(hits) == ct.default_limit

    # @pytest.mark.skip(reason="Todo")
    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_search_after_delete_channel(self):
        """
        target: test search after compact, and queryNode get delete request from channel,
                rather than compacted delta log
        method: 1.insert, flush and load
                2.delete half
                3.compact
                4.search
        expected: No compact, compact get delta log from storage
        """
        collection_w = self.init_collection_wrap(cf.gen_unique_str(prefix), shards_num=1)

        df = cf.gen_default_dataframe_data()
        insert_res, _ = collection_w.insert(df)
        assert collection_w.num_entities == ct.default_nb

        collection_w.load()

        expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys[:ct.default_nb // 2]}'
        collection_w.delete(expr)

        collection_w.compact()
        c_plans = collection_w.get_compaction_plans()[0]
        assert len(c_plans.plans) == 0

        # search
        sleep(2)
        collection_w.load()
        search_res, _ = collection_w.search(cf.gen_vectors(ct.default_nq, ct.default_dim),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit,
                                            check_task=CheckTasks.check_search_results,
                                            check_items={"nq": ct.default_nq,
                                                         "ids": insert_res.primary_keys[ct.default_nb // 2:],
                                                         "limit": ct.default_limit}
                                            )

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_delete_inside_time_travel(self):
        """
        target: test compact inside time_travel range
        method: 1.insert data and get ts
                2.delete all ids
                4.compact
                5.search with ts
        expected: Verify search result
        """
        from pymilvus import utility
        collection_w = self.init_collection_wrap(cf.gen_unique_str(prefix), shards_num=1)

        # insert and get tt
        df = cf.gen_default_dataframe_data(tmp_nb)
        insert_res, _ = collection_w.insert(df)
        tt = utility.mkts_from_hybridts(insert_res.timestamp, milliseconds=0.)

        # delete all
        expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys}'
        delete_res, _ = collection_w.delete(expr)
        log.debug(collection_w.num_entities)

        collection_w.compact()

        collection_w.load()
        search_one, _ = collection_w.search(df[ct.default_float_vec_field_name][:1].to_list(),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit,
                                            travel_timestamp=tt)
        assert 0 in search_one[0].ids

    @pytest.mark.xfail(reason="Issue 12450")
    @pytest.mark.tags(CaseLabel.L3)
    def test_compact_delete_outside_time_travel(self):
        """
        target: test compact outside time_travel range
        method: 1.create and insert
                2.get time stamp
                3.delete
                4.compact after compact_retention_duration
                5.load and search with travel time tt
        expected: Empty search result
        """
        from pymilvus import utility
        collection_w = self.init_collection_wrap(cf.gen_unique_str(prefix), shards_num=1)

        # insert
        df = cf.gen_default_dataframe_data(tmp_nb)
        insert_res, _ = collection_w.insert(df)
        tt = utility.mkts_from_hybridts(insert_res.timestamp, milliseconds=0.)

        expr = f'{ct.default_int64_field_name} in {insert_res.primary_keys}'
        delete_res, _ = collection_w.delete(expr)
        log.debug(collection_w.num_entities)

        # ensure compact remove delta data that delete outside retention range
        # sleep(ct.compact_retention_duration)
        sleep(60)

        collection_w.compact()
        collection_w.load()

        # search with travel_time tt
        search_res, _ = collection_w.search(df[ct.default_float_vec_field_name][:1].to_list(),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit,
                                            travel_timestamp=tt)
        log.debug(search_res[0].ids)
        assert len(search_res[0]) == 0

    @pytest.mark.tags(CaseLabel.L0)
    def test_compact_merge_two_segments(self):
        """
        target: test compact merge two segments
        method: 1.create with shard_num=1
                2.insert and flush
                3.insert and flush again
                4.compact
                5.load
        expected: Verify segments are merged
        """
        num_of_segment = 2
        # create collection shard_num=1, insert 2 segments, each with tmp_nb entities
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, num_of_segment, tmp_nb)

        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans = collection_w.get_compaction_plans()[0]

        # verify the two segments are merged into one
        assert len(c_plans.plans) == 1
        assert len(c_plans.plans[0].sources) == 2
        target = c_plans.plans[0].target

        # verify queryNode load the compacted segments
        collection_w.load()
        segment_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        assert target == segment_info[0].segmentID

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_no_merge(self):
        """
        target: test compact when no segments merge
        method: 1.create with shard_num=1
                2.insert and flush
                3.compact and search
        expected: No exception and no compact plans
        """
        # create collection
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        df = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df)
        assert collection_w.num_entities == tmp_nb

        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans, _ = collection_w.get_compaction_plans()
        assert len(c_plans.plans) == 0

    @pytest.mark.tags(CaseLabel.L1)
    # @pytest.mark.skip(reason="issue #12957")
    def test_compact_manual_and_auto(self):
        """
        target: test compact manual and auto
        method: 1.create with shard_num=1
                2.insert one and flush (11 times)
                3.compact
                4.load and search
        expected: Verify segments info
        """
        # greater than auto-merge threshold 10
        num_of_segment = ct.compact_segment_num_threshold + 1

        # create collection shard_num=1, insert 11 segments, each with one entity
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, num_of_segment=num_of_segment)

        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()[0]

        collection_w.load()
        segments_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        assert len(segments_info) == 1

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_merge_multi_segments(self):
        """
        target: test compact and merge multi small segments
        method: 1.create with shard_num=1
                2.insert one and flush (less than threshold)
                3.compact
                4.load and search
        expected: Verify segments info
        """
        # less than auto-merge threshold 10
        num_of_segment = ct.compact_segment_num_threshold - 1

        # create collection shard_num=1, insert 11 segments, each with one entity
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, num_of_segment=num_of_segment)

        collection_w.compact()
        collection_w.wait_for_compaction_completed()

        c_plans = collection_w.get_compaction_plans()[0]
        assert len(c_plans.plans[0].sources) == num_of_segment
        target = c_plans.plans[0].target

        collection_w.load()
        segments_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        assert len(segments_info) == 1
        assert segments_info[0].segmentID == target

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_merge_inside_time_travel(self):
        """
        target: test compact and merge segments inside time_travel range
        method: search with time travel after merge compact
        expected: Verify segments inside time_travel merged
        """
        from pymilvus import utility
        # create collection shard_num=1, insert 2 segments, each with tmp_nb entities
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)

        # insert twice
        df1 = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df1)[0]
        assert collection_w.num_entities == tmp_nb

        df2 = cf.gen_default_dataframe_data(tmp_nb, start=tmp_nb)
        insert_two = collection_w.insert(df2)[0]
        assert collection_w.num_entities == tmp_nb * 2

        tt = utility.mkts_from_hybridts(insert_two.timestamp, milliseconds=0.1)

        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()[0]

        collection_w.load()
        search_res, _ = collection_w.search(df2[ct.default_float_vec_field_name][:1].to_list(),
                                            ct.default_float_vec_field_name,
                                            ct.default_search_params, ct.default_limit,
                                            travel_timestamp=tt)
        assert tmp_nb in search_res[0].ids
        assert len(search_res[0]) == ct.default_limit

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_threshold_auto_merge(self):
        """
        target: test num (segment_size < 1/2Max) reaches auto-merge threshold 10
        method: 1.create with shard_num=1
                2.insert flush 10 times (merge threshold 10)
                3.wait for compaction, load
        expected: Get query segments info to verify segments auto-merged into one
        """
        threshold = ct.compact_segment_num_threshold

        # create collection shard_num=1, insert 10 segments, each with one entity
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, num_of_segment=threshold)

        # Estimated auto-merging takes 30s
        cost = 60
        collection_w.load()
        start = time()
        while True:
            sleep(5)
            segments_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]

            # verify segments reaches threshold, auto-merge ten segments into one
            if len(segments_info) == 1:
                break
            end = time()
            if end - start > cost:
                raise BaseException(1, "Ccompact auto-merge more than 60s")

    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_less_threshold_no_merge(self):
        """
        target: test compact the num of segments that size less than 1/2Max, does not reach the threshold
        method: 1.create collection with shard_num = 1
                2.insert flush 9 times (segments threshold 10)
                3.after a while, load
        expected: Verify segments are not merged
        """
        less_threshold = ct.compact_segment_num_threshold - 1

        # create collection shard_num=1, insert 9 segments, each with one entity
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, num_of_segment=less_threshold)

        sleep(3)

        # load and verify no auto-merge
        collection_w.load()
        segments_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        assert len(segments_info) == less_threshold

    @pytest.mark.skip(reason="Todo")
    @pytest.mark.tags(CaseLabel.L2)
    def test_compact_multi_collections(self):
        """
        target: test compact multi collections with merge
        method: create 50 collections, add entities into them and compact in turn
        expected: No exception
        """
        pass

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_and_insert(self):
        """
        target: test insert after compact
        method: 1.create and insert with flush
                2.delete and compact
                3.insert new data
                4.load and search
        expected: Verify search result and segment info
        """
        # create collection shard_num=1, insert 2 segments, each with tmp_nb entities
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, nb_of_segment=tmp_nb)

        # compact two segments
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # insert new data, verify insert flush successfully
        df = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df)
        assert collection_w.num_entities == tmp_nb * 3

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_and_delete(self):
        """
        target: test delete after compact
        method: 1.delete half and compact
                2.load and query
                3.delete and query
        expected: Verify deleted ids
        """
        # init collection with one shard, insert into two segments
        collection_w = self.collection_insert_multi_segments_one_shard(prefix, is_dup=False)

        # compact and complete
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        collection_w.get_compaction_plans()

        # delete and query
        expr = f'{ct.default_int64_field_name} in {[0]}'
        collection_w.delete(expr)
        collection_w.load()
        collection_w.query(expr, check_task=CheckTasks.check_query_empty)

        expr_1 = f'{ct.default_int64_field_name} in {[1]}'
        collection_w.query(expr_1, check_task=CheckTasks.check_query_results, check_items={'exp_res': [{'int64': 1}]})

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_cross_shards(self):
        """
        target: test compact cross shards
        method: 1.create with shard_num=2
                2.insert once and flush (two segments, belonging to two shards)
                3.compact and completed
        expected: Verify no compact
        """
        # insert into two segments with two shard
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=2)
        df = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df)
        assert collection_w.num_entities == tmp_nb

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed(timeout=1)
        c_plans = collection_w.get_compaction_plans()[0]

        # Actually no merged
        assert len(c_plans.plans) == 0

    @pytest.mark.tags(CaseLabel.L1)
    def test_compact_cross_partition(self):
        """
        target: test compact cross partitions
        method: 1.create with shard_num=1
                2.create partition and insert, flush
                3.insert _default partition and flush
                4.compact
        expected: Verify no compact
        """
        # create collection and partition
        collection_w = self.init_collection_wrap(name=cf.gen_unique_str(prefix), shards_num=1)
        partition_w = self.init_partition_wrap(collection_wrap=collection_w)

        # insert
        df = cf.gen_default_dataframe_data(tmp_nb)
        collection_w.insert(df)
        assert collection_w.num_entities == tmp_nb
        partition_w.insert(df)
        assert collection_w.num_entities == tmp_nb * 2

        # compact
        collection_w.compact()
        collection_w.wait_for_compaction_completed()
        c_plans = collection_w.get_compaction_plans()[0]

        # Actually no merged
        assert len(c_plans.plans) == 0
        collection_w.load()
        segments_info = self.utility_wrap.get_query_segment_info(collection_w.name)[0]
        assert segments_info[0].partitionID != segments_info[-1].partitionID
