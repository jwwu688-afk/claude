/* ===== 实体与几何：纯数据层，不引用 DOM，浏览器与 Node 共用。 =====
 * 【2026-11-10】碰撞采用圆形判定：飞机装饰翼尖不参与，给躲弹留余地。
 * 所有速度单位为逻辑像素/秒，计时单位为秒；与屏幕 DPR 无关。
 */
(function (root) {
  'use strict';
  const P = root.Plane = root.Plane || {};
  // 480×720 是固定逻辑战场；CSS 缩放不改变难度与碰撞范围。
  P.W = 480; P.H = 720;
  /** 限制数值到闭区间；调用方保证输入是有限数。 */
  P.clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  /** 输入两个带 x/y/r 的实体；相切算命中，死亡实体永不重复碰撞。 */
  P.hit = (a, b) => !a.dead && !b.dead && Math.hypot(a.x - b.x, a.y - b.y) <= a.r + b.r;
  /** 创建玩家；不接受外部状态，重开不会继承上一局强化。 */
  P.player = () => ({ x: 240, y: 610, r: 11, lives: 3, bombs: 2, inv: 2, shield: 0, weapon: 1, power: 0, fire: 0 });
  /** type 为 scout/weaver/tank/boss；level 至少 1，rng 返回 [0,1)。
   * 出生于屏幕上方；Boss 固定中点，普通敌机留出两侧安全间距。
   */
  P.enemy = (type, level, rng) => {
    const spec = { scout: [2, 18, 110, 100], weaver: [4, 21, 83, 180], tank: [9, 29, 49, 350], boss: [140 + level * 35, 61, 30, 3000] }[type];
    return { type, x: type === 'boss' ? 240 : 40 + rng() * 400, y: -75, r: spec[1], hp: spec[0] + (type === 'boss' ? 0 : Math.floor(level / 3)), maxHp: spec[0] + (type === 'boss' ? 0 : Math.floor(level / 3)), speed: spec[2] + Math.min(50, level * 4), score: spec[3], age: 0, fire: 1, phase: rng() * Math.PI * 2, flash: 0, dead: false };
  };
  /** 子弹保持独立 dead 标记；一发子弹最多击中一个目标。 */
  P.bullet = (x, y, vx, vy, hostile = false) => ({ x, y, vx, vy, r: hostile ? 5 : 4, hostile, dead: false });
  /** 补给种类由核心指定，金色外观由渲染层负责。 */
  P.pickup = (x, y, type) => ({ x, y, type, r: 16, age: 0, dead: false });
  // 条件导出不会在 file:// 的经典 script 中触发 require。
  if (typeof module !== 'undefined' && module.exports) module.exports = P;
})(globalThis);
