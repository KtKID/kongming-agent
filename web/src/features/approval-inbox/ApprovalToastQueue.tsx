import { AnimatePresence, motion } from "framer-motion";
import { useShallow } from "zustand/react/shallow";
import { ApprovalToastCard } from "./ApprovalToastCard";
import { selectSorted, useApprovalInboxStore } from "./useApprovalInbox";

/**
 * 右下角全局浮窗队列。
 *
 * 容器：``fixed bottom-4 right-4 z-50``；``pointer-events-none`` 让容器不挡其他点击，
 * 卡片自己 ``pointer-events-auto``（在 Card 内）。
 *
 * 内部用 ``<AnimatePresence>`` 包裹 ``<motion.ul layout>``：
 * - ``flex-col`` —— **数组顺序 = 视觉顺序（自上而下）**；危险卡（``selectSorted`` 已置顶）渲染在最上，
 *   同组内旧卡在上、新卡在下。新审批走 ``initial y:24`` 从自身位置下方滑入到位，
 *   配合 ``layout`` 把已有卡片自然往上顶（README L11「从底部加入把旧卡片往上顶」语义）
 * - 入场：``opacity 0 + y:24 → 1 + 0``（0.24s easeOut）
 * - 退场：``opacity 0 + x:120``（向右划出）
 * - ``layout`` 让卡片之间的位移有过渡（删除一张时其他自动补位）
 *
 * 上限：``MAX_VISIBLE = 50`` —— 队列空 → 不渲染（避免空 motion 容器消耗）。
 * 超出时附 "+N more" 提示。
 *
 * 挂载位置（#8 的活）：``Layout.tsx`` 全局挂一个。
 */

const MAX_VISIBLE = 50;

export function ApprovalToastQueue() {
  // hardening：selectSorted 每次返回新数组；用 useShallow 浅比较结果，
  // 否则 zustand v5 + React 19 useSyncExternalStore 二阶段 snapshot 比对
  // 会判定"状态变了"导致 Maximum update depth exceeded 死循环。
  const items = useApprovalInboxStore(useShallow(selectSorted));

  if (items.length === 0) return null;

  const visible = items.slice(0, MAX_VISIBLE);
  const overflowCount = items.length - visible.length;

  return (
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-h-[80vh] flex-col gap-2 overflow-hidden"
      data-testid="approval-inbox-queue"
      data-count={items.length}
    >
      {overflowCount > 0 && (
        <div
          className="pointer-events-auto self-end rounded-md bg-muted/90 px-2 py-1 text-[10px] text-muted-foreground shadow"
          data-testid="approval-inbox-overflow"
        >
          +{overflowCount} more
        </div>
      )}
      <AnimatePresence initial={false}>
        <motion.ul
          layout
          className="flex flex-col gap-2 list-none m-0 p-0"
        >
          {visible.map((item) => (
            <motion.li
              key={item.requestId}
              layout
              initial={{ opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, x: 120 }}
              transition={{ duration: 0.24, ease: "easeOut" }}
              data-testid="approval-inbox-card-wrapper"
              data-request-id={item.requestId}
            >
              <ApprovalToastCard item={item} />
            </motion.li>
          ))}
        </motion.ul>
      </AnimatePresence>
    </div>
  );
}
