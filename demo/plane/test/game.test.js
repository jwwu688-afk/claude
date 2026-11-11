/* ===== 零依赖逻辑回归：断言玩家可观察的规则，失败由 Node 非零退出。 ===== */
'use strict';
const assert = require('node:assert/strict');
const P = require('../js/entities.js');
const Game = require('../js/game.js');
let passed = 0;
/** 每个测试独立创建游戏，避免随机数与上一例状态互相干扰。 */
function test(name, fn) { fn(); passed++; console.log(`PASS ${name}`); }
const game = () => { const g = new Game(() => 0.5); g.reset(); return g; };
test('初始生命与炸弹', () => { const g = game(); assert.equal(g.p.lives, 3); assert.equal(g.p.bombs, 2); });
test('暂停冻结计时与射击', () => { const g = game(); g.pause(); g.update(0.05); assert.equal(g.time, 0); assert.equal(g.shots.length, 0); });
test('对角线移动不加速', () => { const a = game(), b = game(); a.update(0.05, { x: 1 }); b.update(0.05, { x: 1, y: 1 }); assert.ok(Math.abs(Math.hypot(b.p.x - 240, b.p.y - 610) - (a.p.x - 240)) < 1e-8); });
test('玩家不能飞出战场', () => { const g = game(); for (let i = 0; i < 100; i++) g.update(0.05, { x: -1, y: 1 }); assert.equal(g.p.x, 22); assert.equal(g.p.y, 694); });
test('切回页面不累积巨大时间步', () => { const g = game(); g.update(100); assert.equal(g.time, 0.05); });
test('子弹只给一个敌机伤害', () => { const g = game(); g.p.fire = 10; const a = P.enemy('scout', 1, () => 0.5), b = P.enemy('scout', 1, () => 0.5); a.y = b.y = 200; g.enemies = [a, b]; g.shots = [P.bullet(240, 200, 0, 0)]; g.update(0); assert.equal(a.hp + b.hp, 3); });
test('击杀不重复计分', () => { const g = game(), e = P.enemy('scout', 1, () => 0.5); g.kill(e); g.kill(e); assert.equal(g.score, 100); });
test('无敌不扣生命', () => { const g = game(); g.damage(); assert.equal(g.p.lives, 3); });
test('护盾先于生命消耗', () => { const g = game(); g.p.inv = 0; g.p.shield = 5; g.damage(); assert.equal(g.p.lives, 3); assert.equal(g.p.shield, 0); });
test('生命耗尽结算', () => { const g = game(); g.p.inv = 0; g.p.lives = 1; g.damage(); assert.equal(g.state, 'over'); });
test('补给上限与火力到期', () => { const g = game(); for (let i = 0; i < 10; i++) for (const type of ['power', 'heal', 'bomb']) g.collect(P.pickup(0, 0, type)); assert.equal(g.p.weapon, 3); assert.equal(g.p.lives, 3); assert.equal(g.p.bombs, 5); g.p.power = 0.01; g.update(0.05); assert.equal(g.p.weapon, 1); });
test('炸弹清除敌弹并重创Boss', () => { const g = game(); const e = P.enemy('boss', 1, () => 0.5); g.enemies = [e]; g.boss = e; g.bullets = [P.bullet(200, 300, 0, 1, true)]; const hp = e.hp; assert.equal(g.bomb(), true); assert.equal(e.hp, hp - 45); assert.equal(g.bullets.length, 0); assert.equal(g.p.bombs, 1); });
test('暂停与空库存不能投弹', () => { const g = game(); g.pause(); assert.equal(g.bomb(), false); g.pause(); g.p.bombs = 0; assert.equal(g.bomb(), false); });
test('Boss不重复出现且击落后延时刷新', () => { const g = game(); g.time = 35; g.update(0); const boss = g.boss; g.update(0); assert.equal(g.enemies.filter(e => e.type === 'boss').length, 1); g.kill(boss); assert.equal(g.nextBoss, 70); g.update(0); assert.equal(g.boss, null); });
test('重新开始清空旧弹幕和强化', () => { const g = game(); g.score = 900; g.p.weapon = 3; g.update(0.05); g.reset(); assert.equal(g.score, 0); assert.equal(g.shots.length, 0); assert.equal(g.p.weapon, 1); });
test('连击计数正确递增', () => { const g = game(); const e1 = P.enemy('scout', 1, () => 0.5); g.kill(e1); assert.equal(g.combo, 1); const e2 = P.enemy('scout', 1, () => 0.5); g.kill(e2); assert.equal(g.combo, 2); });
test('连击倍率正确计算', () => { const g = game(); for (let i = 0; i < 10; i++) g.kill(P.enemy('scout', 1, () => 0.5)); assert.equal(g.combo, 10); assert.equal(g.comboMul, 2); for (let i = 0; i < 10; i++) g.kill(P.enemy('scout', 1, () => 0.5)); assert.equal(g.combo, 20); assert.equal(g.comboMul, 3); });
test('受伤时连击清零', () => { const g = game(); g.kill(P.enemy('scout', 1, () => 0.5)); g.kill(P.enemy('scout', 1, () => 0.5)); assert.equal(g.combo, 2); g.p.inv = 0; g.damage(); assert.equal(g.combo, 0); });
test('超时后连击清零', () => { const g = game(); g.kill(P.enemy('scout', 1, () => 0.5)); g.p.fire = 1000; for (let i = 0; i < 60; i++) g.update(0.05); assert.equal(g.combo, 0); });
test('暂停时连击不清零', () => { const g = game(); g.kill(P.enemy('scout', 1, () => 0.5)); g.pause(); g.update(10); assert.equal(g.combo, 1); });
test('连击倍率影响分数', () => { const g = game(); const e = P.enemy('scout', 1, () => 0.5); for (let i = 0; i < 10; i++) g.kill(P.enemy('scout', 1, () => 0.5)); const hp = e.hp; const before = g.score; g.kill(e); assert.equal(g.score - before, 200); });
console.log(`所有测试通过（${passed}/${passed}）`);
