/**
 * track-timeline-v0.1 · TrajectoryStore 状态机测试。
 *
 * 覆盖：加载三态写入、选中 / 搜索 / 折叠状态独立存在（feat05 场景3：
 * 清空搜索后选中与折叠原样恢复）、工具调用一键收起、resetThread 清理。
 */

import { beforeEach, describe, expect, it } from "vitest";
import type { TrajectoryResponse } from "@/protocol";
import { useTrajectoryStore } from "../TrajectoryStore";

function emptyResponse(): TrajectoryResponse {
  return {
    thread_id: "thread-abcdef012345",
    summary: {
      status: "completed",
      total_duration_ms: 0,
      turn_count: 0,
      tool_call_count: 0,
      corrupted_count: 0,
      tokens: {},
    },
    runs: [],
    records: [],
  };
}

beforeEach(() => {
  // 重置为初始空 store（zustand setState 全量替换）
  useTrajectoryStore.setState({
    dataByThread: {},
    loadingByThread: {},
    errorByThread: {},
    selectedSeqByThread: {},
    searchByThread: {},
    collapsedTurnsByThread: {},
    toolsCollapsedByThread: {},
  });
});

describe("加载三态", () => {
  it("begin → finish 写入数据并清错误", () => {
    const store = useTrajectoryStore.getState();
    store.beginLoad("t1");
    expect(useTrajectoryStore.getState().loadingByThread.t1).toBe(true);
    useTrajectoryStore.getState().failLoad("t1", "not_found");
    expect(useTrajectoryStore.getState().loadingByThread.t1).toBe(false);
    expect(useTrajectoryStore.getState().errorByThread.t1).toBe("not_found");
    useTrajectoryStore.getState().beginLoad("t1");
    useTrajectoryStore.getState().finishLoad("t1", emptyResponse());
    const state = useTrajectoryStore.getState();
    expect(state.errorByThread.t1).toBeUndefined();
    expect(state.dataByThread.t1?.thread_id).toBe("thread-abcdef012345");
  });
});

describe("选中 / 搜索 / 折叠独立于过滤存在", () => {
  it("设置搜索与折叠后清空搜索，选中与折叠不变（feat05 场景3）", () => {
    const store = useTrajectoryStore.getState();
    store.select("t1", 7);
    store.setSearch("t1", "runner.py");
    store.toggleTurnCollapsed("t1", "1:1");
    store.setToolsCollapsed("t1", true);

    useTrajectoryStore.getState().setSearch("t1", "");
    const state = useTrajectoryStore.getState();
    expect(state.selectedSeqByThread.t1).toBe(7);
    expect(state.collapsedTurnsByThread.t1).toEqual(["1:1"]);
    expect(state.toolsCollapsedByThread.t1).toBe(true);
    expect(state.searchByThread.t1).toBe("");
  });

  it("toggleTurnCollapsed 再点一次恢复展开；一键全部折叠 / 展开", () => {
    useTrajectoryStore.getState().finishLoad("t1", {
      ...emptyResponse(),
      records: [
        {
          seq: 1,
          kind: "user_input",
          run_index: 1,
          run_id: "r",
          turn: 1,
          summary: "",
          started_at_ms: 0,
          duration_ms: null,
          ok: null,
          tool_name: null,
          call_id: null,
          ttft_ms: null,
          finish_reason: null,
          tokens: null,
          input_detail: null,
          output_detail: null,
          reasoning_detail: null,
          error_message: null,
        },
      ],
    });
    useTrajectoryStore.getState().toggleTurnCollapsed("t1", "1:1");
    expect(useTrajectoryStore.getState().collapsedTurnsByThread.t1).toEqual(["1:1"]);
    useTrajectoryStore.getState().toggleTurnCollapsed("t1", "1:1");
    expect(useTrajectoryStore.getState().collapsedTurnsByThread.t1).toEqual([]);

    useTrajectoryStore.getState().setAllTurnsCollapsed("t1", true);
    expect(useTrajectoryStore.getState().collapsedTurnsByThread.t1).toEqual(["1:1"]);
    useTrajectoryStore.getState().setAllTurnsCollapsed("t1", false);
    expect(useTrajectoryStore.getState().collapsedTurnsByThread.t1).toEqual([]);
  });

  it("resetThread 清掉该 thread 全部状态", () => {
    useTrajectoryStore.getState().finishLoad("t1", emptyResponse());
    useTrajectoryStore.getState().select("t1", 3);
    useTrajectoryStore.getState().resetThread("t1");
    const state = useTrajectoryStore.getState();
    expect(state.dataByThread.t1).toBeUndefined();
    expect(state.selectedSeqByThread.t1).toBeUndefined();
  });
});
