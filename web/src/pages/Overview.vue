<template>
  <div class="overview">
    <div class="resmon">
      <span>内存 {{ resmon.used_mb ?? '-' }} / {{ resmon.total_mb ?? '-' }} MB</span>
      <div class="bar"><div class="fill" :class="{alert: resmon.alert}"
        :style="{width: ((resmon.ratio||0)*100).toFixed(1) + '%'}"/></div>
      <button @click="scan">扫描安装目录</button>
    </div>
    <div v-if="scanRes" class="scanbox">
      <!-- 扫描结果目录可能几十条，压在侧栏上方把真正要操作的按钮顶出视口。
           给它一个折叠开关，默认收起（扫描是「偶尔看一眼」，不是常驻信息）。 -->
      <div class="ops scan-toggle">
        <button @click="scanOpen = !scanOpen">
          {{ scanOpen ? '▾ 收起扫描结果' : `▸ 安装目录（${scanRes.dirs.length}）` }}
        </button>
      </div>
      <template v-if="scanOpen">
      <p class="hint">安装根：{{ scanRes.roots.join('、') }}
        （owned = 实例受管；orphan = 匹配程序名但无实例记录，可清理；
          external = 无关目录，仅展示）</p>
      <div v-for="d in scanRes.dirs" :key="d.path" class="scan-row">
        <span v-if="d.kind === 'owned'" class="kind-owned">✓ {{ d.path }}
          — {{ d.dice }} · {{ d.id }}（{{ d.state }}）</span>
        <template v-else-if="d.kind === 'orphan'">
          <span class="kind-orphan">⚠ {{ d.path }} — 游离目录（更新于 {{ d.mtime }}）</span>
          <button class="danger" @click="delOrphan(d.path)">删除</button>
        </template>
        <span v-else class="kind-ext">{{ d.path }} — 无关目录（{{ d.mtime }}）</span>
      </div>
      <div v-for="p in scanRes.procs" :key="p.pid" class="scan-row">
        <span :class="p.owner ? 'kind-owned' : (p.killable ? 'kind-orphan' : 'kind-ext')">PID {{ p.pid }}
          {{ p.cmd || p.exe }}{{ p.owner ? ' — 实例 ' + p.owner : (p.killable ? ' — 游离进程' : ' — 无关进程') }}</span>
        <button v-if="!p.owner && p.killable" class="danger" @click="killProc(p.pid)">结束</button>
      </div>
      <p v-if="!scanRes.dirs.length && !scanRes.procs.length" class="hint">
        安装根下没有目录与相关进程。</p>
      </template>
    </div>
    <p v-if="!nodes.length && !connected" class="hint">正在连接服务端…</p>
    <p v-else-if="!nodes.length" class="hint">
      还没有实例 —— 点上方「创建」选择应用端或登录端开始，或直接访问 <code>#/wizard</code>。
    </p>
    <div class="layout">
      <div class="topo-wrap">
    <svg :viewBox="'0 0 ' + vbW + ' 440'" class="topo">
      <text :x="colX.login" y="24" class="col-title" text-anchor="middle">登录端</text>
      <text :x="colX.dice" y="24" class="col-title" text-anchor="middle">应用端</text>
      <text v-if="!loginNodes.length" :x="colX.login" y="215" class="col-empty" text-anchor="middle">（暂无）</text>
      <text v-if="!diceNodes.length" :x="colX.dice" y="215" class="col-empty" text-anchor="middle">（暂无）</text>
      <line v-for="e in edges" :key="e.src+e.dst"
            :x1="pos(e.src).x" :y1="pos(e.src).y"
            :x2="pos(e.dst).x" :y2="pos(e.dst).y" :class="e.state"/>
      <!-- 连线中点标注状态，颜色随线；白描边保证压在线上也可读 -->
      <text v-for="e in edges" :key="'t'+e.src+e.dst+(e.account_qq||'')"
            :x="(pos(e.src).x + pos(e.dst).x) / 2"
            :y="(pos(e.src).y + pos(e.dst).y) / 2 - 7"
            :class="['edge-label', e.state]" text-anchor="middle">{{
        stateText(e.state) }}<tspan v-if="e.account_qq"> · QQ {{ e.account_qq }}</tspan></text>
      <g v-for="n in nodes" :key="n.id"
         :transform="`translate(${pos(n.id).x},${pos(n.id).y})`" @click="sel = n">
        <rect x="-70" y="-26" width="140" height="52" rx="8"
              :class="{dead: !n.process_alive}"/>
        <text y="-6">{{ n.dice }}{{ n.arch === 'allinone' ? '（整合包）' : '' }}</text>
        <text y="14" class="sub">{{ n.state }} : {{ n.port || '-' }}{{ n.mem_mb ? ' · ' + n.mem_mb + 'MB' : '' }}</text>
        <text v-if="n.crash_looped" y="40" class="warn">反复崩溃，已停止自动重启</text>
        <text v-for="(w, i) in n.warnings" :key="i" :y="n.crash_looped ? 54 : 40" class="warn">{{ w }}</text>
        <!-- 高频操作内联到节点上：启停/重启在方框正下方，管理应用在方框右侧。
             这四个动作占侧栏按钮的一大半，却藏在要滚动才看得到的地方；
             放到方框边上后，「选谁」和「对它做什么」在同一处。
             stop() 必须 @click.stop —— 不阻止冒泡的话点停止会同时把 sel 切成该节点，
             点了「停」却顺带切换选中对象，与用户预期不符。 -->
        <g class="node-ops" @click.stop>
          <!-- ⚠️ SVG 没有「子元素相对父 rect 定位」这回事：所有图标的坐标都是
               **节点级绝对坐标**。方块在 y=24（中心 34），图标就必须带这个偏移，
               写成 M-4.5 -5.5 会画到节点中心（方框里）去 —— 表现为「方块是空的」。
               偏移量：方块中心 x = -26 / 0 / 26，y = 34。 -->
          <rect :x="-36" y="24" width="20" height="20" rx="4" class="qbtn"
                @click="quickOp(n.id, 'start')"
                :class="{off: n.state === 'RUNNING'}">
            <path d="M-30.5 28.5 L-21 34 L-30.5 39.5 Z" class="qico"/>
            <title>启动 {{ n.dice }}</title></rect>
          <rect x="-10" y="24" width="20" height="20" rx="4" class="qbtn"
                @click="quickOp(n.id, 'stop')"
                :class="{off: n.state !== 'RUNNING'}">
            <rect x="-4.5" y="29.5" width="9" height="9" class="qico"/>
            <title>停止 {{ n.dice }}</title></rect>
          <!-- 重启：圆弧圆心与方块中心 (26,34) 重合（半径 5），缺口开在右侧，
         箭头贴在圆弧右上端点。图标不必追求几何精确居中 —— 描边图标本身
         有视觉补偿，1~2px 偏差肉眼不可见，别为它反复重算坐标。 -->
          <rect x="16" y="24" width="20" height="20" rx="4" class="qbtn"
                @click="quickOp(n.id, 'restart')">
            <path d="M28.9 29.9 A5 5 0 1 0 28.9 38.1" class="qico qico-stroke"/>
            <path d="M27.3 32.2 L27.2 28 L31.2 30.8 Z" class="qico"/>
            <title>重启 {{ n.dice }}</title></rect>
        </g>
        <!-- 管理应用放右侧：与启停分开，避免「四个方块里哪个是管理」要认位置。
             用方形而非圆形，与下方的启停组保持同一视觉语言；
             描边式齿轮在方块里比实心齿轮更清晰（方块面积小，实心会糊成一团）。 -->
        <rect x="77" y="-10" width="20" height="20" rx="4" class="qbtn qbtn-side"
              @click.stop="quickManage(n)">
          <circle cx="87" cy="0" r="2.6" class="qico-stroke"/>
          <circle cx="87" cy="0" r="6.6" class="qico-stroke qico-dashed"/>
          <title>管理应用（{{ n.dice }}）</title></rect>
      </g>
    </svg>
    <div class="legend">
      <span><i class="lg-line lg-green"/>已连接</span>
      <span><i class="lg-line lg-gray"/>已配置未连接</span>
      <span><i class="lg-line lg-red"/>连接失败</span>
    </div>
    <!-- 缓存/备份管理（自 Wizard 移入）：程序包与备份产物都长期驻留，集中在拓扑下方展示占用
         与是否仍被实例使用；有死缓存/超龄备份时默认展开提醒 -->
    <details class="cache-box" :open="unusedRows.length > 0">
      <summary>本地缓存（{{ pkgTotalMb }} MB）{{ unusedRows.length
        ? ' · ' + unusedRows.length + ' 个未使用' : ' · 点击管理' }}</summary>
      <p v-if="!pkgRows.length" class="hint">暂无本地缓存。</p>
      <div v-for="r in pkgRows" :key="r.dice" class="pkg">
        <span :class="r.in_use ? 'pkg-ok' : 'pkg-idle'">
          {{ r.dice }} · {{ r.size_mb }} MB ·
          {{ r.source === 'upload' ? '上传' : '下载' }}于 {{ r.updated_at }} —
          {{ r.in_use ? '已有实例在用' : '无实例使用（死缓存，可安全删除）' }}</span>
        <button :disabled="cacheBusy" @click="removePkgOf(r.dice)">删除</button>
      </div>
      <div class="ops" v-if="unusedRows.length">
        <button class="danger" :disabled="cacheBusy" @click="cleanUnused">
          清理 {{ unusedRows.length }} 个未使用包（释放约 {{ unusedMb }} MB）</button>
      </div>
      <p class="hint">删除只是清掉种子包，已部署的实例不受影响；再次部署该程序需重新下载或上传。</p>
      <p v-if="pkgMsg2" class="hint">{{ pkgMsg2 }}</p>
    </details>
    <details class="cache-box" :open="expStaleRows.length > 0">
      <summary>备份文件（{{ expTotalMb }} MB）{{ expStaleRows.length
        ? ' · ' + expStaleRows.length + ' 份超龄' : ' · 点击管理' }}</summary>
      <p v-if="!expRows.length" class="hint">暂无备份文件。</p>
      <div v-for="r in expRows" :key="r.name" class="pkg">
        <span :class="r.age_days >= pruneDays ? 'pkg-idle' : 'pkg-ok'">
          {{ r.name }} · {{ r.size_mb }} MB · {{ r.mtime }}（已存放 {{ r.age_days }} 天）</span>
        <button :disabled="cacheBusy" @click="delExportFile(r.name)">删除</button>
      </div>
      <div class="ops" v-if="expRows.length">
        <label class="inline">清理超过
          <input type="number" min="1" max="365" v-model.number="pruneDays" style="width:64px"/>
          天的备份</label>
        <button class="danger" :disabled="cacheBusy" @click="pruneOldExports">
          清理 {{ expStaleRows.length }} 份（释放约
          {{ Math.round(expStaleRows.reduce((s, r) => s + (r.size_mb || 0), 0)) }} MB）</button>
      </div>
      <p class="hint">升级前会自动留一份整目录快照，定时备份也在这里；确认回滚无需要的旧备份可安全清理。</p>
      <p v-if="expMsg" class="hint">{{ expMsg }}</p>
    </details>
      </div>
      <!-- 侧栏常驻：以前 v-if="sel" 让它整个消失，于是
           「点拓扑图方框右侧的⚙」在未选中时毫无反馈（面板块随之一起消失）。
           现在常驻显示；未选中实例时给一句「点方框选实例」，
           拓扑图也相应留出固定宽度（见 .topo-wrap 的说明）。 -->
      <aside class="panel">
        <div class="panel-top">
          <b>实例操作</b>
          <button v-if="sel" class="x" title="取消选择" @click="sel = null">×</button>
        </div>
        <template v-if="!sel">
          <p class="hint sel-empty">
            点左侧拓扑图里的实例方框（或方框下方的 ▶ 启动）来选择实例。<br/>
            选好后这里会出现该实例的操作按钮。
          </p>
        </template>
        <template v-else>
        <div class="sel-info">
        <b>{{ sel.dice }}</b> · {{ live.state }} · 端口 {{ live.port || '-' }}
        <span v-if="live.qq"> · QQ {{ live.qq }}</span>
        <span v-if="live.mem_mb"> · 内存 {{ live.mem_mb }} MB</span>
        <span v-if="live.bot_mode === 'official'" class="hint"> · 官方机器人通道</span>
        <span v-if="!isLoginSel && linkRows.length"> · 已关联 {{ linkRows.length }} 个登录端</span>
        <span v-else-if="isLoginSel && (selLive.accounts || []).length"> · 已登录 {{ (selLive.accounts || []).length }} 个账号</span>
        <span v-if="live.crash_looped" class="crash-warn"> · ⚠ 反复崩溃已熔断，请查看日志后手动启动</span>
        <span class="iid" title="点击复制实例 ID" @click="copyId">{{ sel.id }} ⧉</span>
      </div>
      <!-- 侧栏按钮分两层：
           ① 面板按钮（可展开/收起）：管理应用 / 连接管理 / 定时任务 / 资源曲线
              —— 点开是一块内容，再点收起来。统一放最前，因为它们是「看信息」的入口。
           ② 直接动作：日志 / 更新 / 文件 / 备份 / 删除 —— 点完即生效，无需二次点击。
           高频的启停重启+管理已内联到拓扑图方框（见上方 SVG），此处不重复。 -->
      <div class="ops">
        <button :class="{ on: manageOpen }" @click="quickManage()">管理应用</button>
        <button :class="{ on: connOpen }" @click="togglePanel('conn')">
          连接管理<template v-if="!isLoginSel && linkRows.length"> ({{ linkRows.length }})</template></button>
        <button :class="{ on: schedOpen }" @click="togglePanel('sched')">
          定时任务<template v-if="schedules.length"> ({{ schedules.length }})</template></button>
        <button :class="{ on: showMetrics }" @click="toggleMetrics">资源曲线</button>
        <button v-if="linkTarget" @click="runDiagnose">诊断连接</button>
      </div>
      <div class="ops">
        <button @click="goLogs">查看日志</button>
        <button @click="checkUpgrade">检查更新</button>
        <button v-if="upgradeLatest" @click="doUpgrade">{{ upgradeLatest ? `升级到 ${upgradeLatest}` : '升级依赖' }}</button>
      </div>
      <div class="ops">
        <button v-if="!isServer" @click="openFolder" title="在文件管理器中打开实例目录">打开文件夹</button>
        <button @click="pickBackup">上传备份</button>
      </div>
      <!-- 备份导出：两个范围会导出完全不同的东西，标题写清差别 -->
      <div class="ops">
        <button @click="doExport('full')" title="程序+配置+存档+数据全部打包">导出整目录</button>
        <button @click="doExport('data')" title="仅应用数据/存档；恢复前提是实例已部署同版本程序">仅导出数据</button>
      </div>
      <div class="ops">
        <button v-if="uploading" class="danger" @click="cancelUpload">取消上传</button>
        <button class="danger" @click="del">删除实例</button>
      </div>
      <input ref="backupInput" type="file" hidden
             accept=".zip,.tgz,.tar,.tar.gz,.tar.xz,.tar.bz2" @change="doBackup"/>
      <p v-if="webuiInfo" class="hint">
        WebUI 登录令牌：<code class="tok" title="点击复制" @click="copyToken">{{
          webuiInfo.token || '日志中未发现令牌，请进 WebUI 查看' }}</code>（点击复制）</p>
      <!-- 资源曲线：24h 内存/CPU 走势（采样 60s 一点，面板重启不丢历史） -->
      <div v-if="showMetrics" class="metrics">
        <div class="metrics-top">
          <b>资源曲线</b>
          <span class="hint">{{ metricsHours }} 小时</span>
          <button @click="loadMetrics">刷新</button>
        </div>
        <p v-if="!chartPts.length" class="hint">
          暂无采样点（实例刚部署或长时间未运行，采样进程每分钟记录一次）。</p>
        <template v-else>
          <svg :viewBox="`0 0 ${chartW} ${chartH}`" class="chart" preserveAspectRatio="none">
            <polyline :points="memPath" class="line-mem" fill="none"/>
            <polyline :points="cpuPath" class="line-cpu" fill="none"/>
          </svg>
          <div class="metrics-legend">
            <span><i class="lg-line line-mem"/>内存
              最新 {{ metrics?.latest_mem_mb ?? '-' }} MB ·
              峰值 {{ metrics?.peak_mem_mb ?? '-' }} MB</span>
            <span><i class="lg-line line-cpu"/>CPU
              均值 {{ metrics?.avg_cpu ?? '-' }}% · 上限 {{ cpuMax }}%</span>
          </div>
          <p class="hint">{{ chartRange }}</p>
        </template>
      </div>
      <div v-if="diag" class="diag">
        <p class="hint">互联诊断结果（{{ diag.ok ? '全部通过' : '存在问题' }}）：</p>
        <p v-for="(d, i) in diag.items" :key="i" :class="d.ok ? 'diag-ok' : 'diag-bad'">
          {{ d.ok ? '✓' : '✗' }} {{ d.detail }}</p>
      </div>
      <div v-if="schedOpen" class="sched">
        <p class="hint" style="margin:4px 0">每 X 天 X 小时自动执行一次</p>
        <div v-for="s in schedules" :key="s.id" class="sched-row">
          <span>每{{ s.every_days }}天{{ s.every_hours }}小时
            · {{ s.kind === 'restart' ? '定时重启'
                : '定时备份（' + (s.scope === 'full' ? '整目录' : '数据') + '）' }}
            <template v-if="s.inst_id === sel.id">· 本实例</template></span>
          <button @click="runSched(s)">立即执行</button>
          <button class="danger" @click="delSched(s)">删除</button>
        </div>
        <p v-if="!schedules.length" class="hint">暂无定时任务。</p>
        <div class="sched-add">
          <select v-model="newSched.kind">
            <option value="restart">定时重启</option>
            <option value="backup">定时备份</option>
          </select>
          <select v-if="newSched.kind === 'backup'" v-model="newSched.scope">
            <option value="data">数据备份</option>
            <option value="full">整目录备份</option>
          </select>
          <label class="hint" style="margin:0">每</label>
          <input v-model.number="newSched.days" type="number" min="0" max="365" style="width:70px"/>
          <label class="hint" style="margin:0">天</label>
          <input v-model.number="newSched.hours" type="number" min="0" max="23" style="width:70px"/>
          <label class="hint" style="margin:0">小时</label>
          <button :disabled="!(newSched.days > 0 || newSched.hours > 0)" @click="addSched">添加</button>
        </div>
      </div>
      <!-- ===== 连接管理：支持多连一 / 一连多，按账号分发 ===== -->
      <div v-if="connOpen" class="conn-mgmt">
        <p class="hint" v-if="!isLoginSel">
          关联登录端：每个关联生成一条「应用端 ↔ 登录端」连接；可绑定登录端的某个 QQ 账号，
          或选「默认账号」由其自动分配（支持一个应用端连多个登录端）。
        </p>
        <!-- 应用端：当前关联列表 -->
        <div v-if="!isLoginSel">
          <div v-for="row in linkRows" :key="row.key" class="link-row">
            <div class="link-main">
              <b>{{ row.loginDice }}</b> · {{ row.loginRef }}
              <span v-if="row.accountQq">（QQ {{ row.accountQq }}）</span>
              <span :class="['link-state', row.state]">{{ stateText(row.state) }}</span>
            </div>
            <div class="field" v-if="needsAccountPick(row.loginRef) && row.accounts.length">
              <label>绑定账号</label>
              <select :value="row.accountQq || ''"
                      @change="e => updateLinkAccount(row.loginRef, row.accountQq, e.target.value || null)">
                <option value="">默认账号（自动分配）</option>
                <option v-for="a in row.accounts" :key="a.qq" :value="String(a.qq)">
                  QQ {{ a.qq }}{{ a.status ? '（' + a.status + '）' : '' }}</option>
              </select>
            </div>
            <div class="ops">
              <button class="danger" @click="removeLink(row.loginRef, row.accountQq)">解除此关联</button>
            </div>
          </div>
          <p v-if="!linkRows.length" class="hint">尚未关联任何登录端。</p>
          <!-- 新增关联 -->
          <div class="add-link" v-if="linkCandidates.length">
            <div class="field">
              <label>新增关联登录端</label>
              <select v-model="newLoginRef">
                <option value="">选择登录端…</option>
                <option v-for="c in linkCandidates" :key="c.id" :value="c.id">
                  {{ c.dice }} · {{ c.id }}{{ c.qq ? ' (QQ ' + c.qq + ')' : '' }}</option>
              </select>
            </div>
            <div class="field" v-if="newLoginRef && needsAccountPick(newLoginRef) && newLoginAccounts.length">
              <label>绑定账号（可选）</label>
              <select v-model="newAccountQq">
                <option value="">默认账号（自动分配）</option>
                <option v-for="a in newLoginAccounts" :key="a.qq" :value="String(a.qq)">
                  QQ {{ a.qq }}{{ a.status ? '（' + a.status + '）' : '' }}</option>
              </select>
            </div>
            <div class="ops">
              <button :disabled="!newLoginRef" @click="addLink">关联</button>
            </div>
          </div>
          <p v-else class="hint">暂无可关联的登录端实例。</p>
        </div>
        <!-- 登录端：展示已登录账号 + 被哪些应用端关联 -->
        <div v-else>
          <p class="hint">已登录账号：</p>
          <div v-for="a in (selLive.accounts || [])" :key="a.qq" class="link-row">
            <div class="link-main"><b>QQ {{ a.qq }}</b>
              <span :class="['link-state', a.status === 'running' ? 'solid-green' : 'dashed-gray']">
                {{ a.status || 'unknown' }}</span>
              <span v-if="a.port" class="hint"> · 端口 {{ a.port }}</span>
            </div>
          </div>
          <p v-if="!(selLive.accounts || []).length" class="hint">暂无已登录账号（可能尚未登录）。</p>
          <p class="hint" style="margin-top:8px">被以下应用端关联：</p>
          <div v-for="d in linkedBy" :key="d.id" class="link-row">
            <div class="link-main">{{ d.dice }} · {{ d.id }}
              <span v-for="l in (d.links || []).filter(x => x.login_ref === sel.id)"
                    :key="l.account_qq || ''" class="hint">
                · 账号 {{ l.account_qq || '默认' }}</span>
            </div>
          </div>
          <p v-if="!linkedBy.length" class="hint">暂无应用端关联此登录端。</p>
        </div>
        <div class="ops" v-if="!isLoginSel && linkRows.length">
          <button @click="reconn">重写互联配置</button>
          <button class="danger" @click="unlinkAll">解除全部关联</button>
        </div>
      </div>
      <p v-if="connMsg" class="hint">{{ connMsg }}</p>
        </template>
        <!-- 分化 C3：面板重启仅 server 有意义（systemd 拉起）；desktop 是托盘常驻，无此入口。
             刻意放在 v-else 之外：它是**面板级**操作，与选不选实例无关，
             空状态下也该可用（实例卡死时往往正是要重启面板的时候）。 -->
      <div class="panel-ops" v-if="isServer">
        <button class="danger" :disabled="panelRestarting" @click="restartPanelNow">重启面板</button>
        <span class="hint">整体重启管理面板，运行中的实例会自动拉回</span>
      </div>
      </aside>
    </div>
    <div v-if="manageOpen" class="mask" @click.self="manageOpen = false">
      <div class="box manage-box" @click.stop>
        <div class="manage-head">
          <b>管理应用 · {{ sel.dice }}</b>
          <button @click="manageOpen = false">关闭</button>
        </div>
      <p v-if="manageErr" class="hint err">{{ manageErr }}</p>
      <template v-else-if="manage.capabilities && manage.capabilities.length">
        <div v-for="c in manage.capabilities" :key="c.kind" class="manage-cap">
          <b>{{ c.label }}</b>
          <p v-if="c.disabled_reason" class="hint warn">{{ c.disabled_reason }}</p>
          <!-- webui：直接开子窗口（后端已确认有端口才下发这条能力） -->
          <button v-if="c.kind === 'webui'" @click="openWebui">打开 WebUI</button>
          <!-- python_deps：依赖与插件（NoneBot2 这类无自带 WebUI 的程序） -->
          <template v-else-if="c.kind === 'python_deps'">
            <p v-if="c.missing && c.missing.length" class="hint warn">
              缺少依赖：{{ c.missing.join('、') }}
            </p>
            <div class="manage-row">
              <input v-model="pkgSpec" placeholder="包名或 pip 规格，如 nonebot-plugin-alconna"
                     @keyup.enter="doInstallPkg" :disabled="manageBusy"/>
              <button :disabled="manageBusy || !pkgSpec.trim() || sel.state === 'running'"
                      @click="doInstallPkg">{{ manageBusy ? '处理中…' : '安装' }}</button>
              <button :disabled="manageBusy" @click="doSyncPyproject"
                      title="把 libs/ 里现有的包补写进 pyproject.toml（手工装过插件后用）">
                同步依赖声明</button>
            </div>
            <p v-if="sel.state === 'running'" class="hint warn">
              实例运行中无法装/卸插件（替换依赖文件会导致半新半旧的模块状态），请先停止。
            </p>
            <div v-if="depsData" class="pkg-list">
              <p class="hint">
                已装插件（{{ (depsData.packages || []).length }}）：
                <button class="lnk" @click="loadManage">刷新</button>
              </p>
              <p v-if="!(depsData.packages || []).length && !(depsData.dir_plugins || []).length"
                 class="hint">尚未安装插件。nonebot2 生态的插件包名一般以 nonebot-plugin- 开头。</p>
              <div v-for="pk in depsData.packages || []" :key="pk.name" class="pkg-row">
                <code>{{ pk.name }}</code><span class="hint">{{ pk.version }}</span>
                <button class="danger" :disabled="manageBusy" @click="doUninstallPkg(pk.name)">卸载</button>
              </div>
              <p v-if="(depsData.dir_plugins || []).length" class="hint">
                目录形态插件（不在 pip 体系内，请到实例目录自行管理）：
              </p>
              <div v-for="dp in depsData.dir_plugins || []" :key="dp.name" class="pkg-row">
                <code>{{ dp.name }}</code>
              </div>
              <p class="hint">依赖目录：<code>{{ depsData.libs_path }}</code></p>
            </div>
          </template>
        </div>
      </template>
      <p v-else class="hint">该程序未提供可管理的能力。</p>
      <p v-if="manageMsg" class="hint">{{ manageMsg }}</p>
    </div>
    </div>
    <div v-if="panelRestarting" class="restart-overlay">
      <div class="restart-card">
        <b>面板正在重启…</b>
        <p class="hint" style="margin:6px 0 0">等待服务恢复后自动刷新（最长等 60 秒）</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { connectWS } from '../ws'
// 注意：下方已有同名 ref `resmon`（内存水位），此处不再导入 api 的 resmon()，否则重复声明导致构建失败
import { opInstance, delInstance, listManifests, linkInstance, instanceWebui,
         instanceManage, installPackage, uninstallPackage, syncPyproject,
         instanceMetrics, wizardStep,
         scanInstallRoots, deleteOrphanDir, killOrphanProc, uploadBackup,
         exportBackup, diagnoseInstance, listSchedules, addSchedule, delSchedule,
         runSchedule, upgradeCheck, upgradeInstance,
         listPackages, deletePackage, deleteUnusedPackages,
         listExports, deleteExport, pruneExports, restartPanel, getEdition,
         revealInstance } from '../api'

const nodes = ref([]), edges = ref([]), sel = ref(null), resmon = ref({})
const manifests = ref({})               // 程序清单：删除/关联等行为由 manifest 声明驱动
const connected = ref(false)             // WS 是否已连上：用于区分「连接中」与「真的没有实例」
const webuiInfo = ref(null), connMsg = ref('')
let sock
listManifests().then(m => (manifests.value = m)).catch(() => {})   // 拉不到不阻塞总览

// ---------- 缓存 / 备份管理（自 Wizard 移入，拓扑图下方） ----------
const pkgs = ref({})                    // {dice: {size_mb,source,updated_at,in_use}}
const cacheBusy = ref(false), pkgMsg2 = ref('')
// 缓存管理视图：全部本地包 / 未被任何实例使用的那部分（可安全删除的死缓存）
const pkgRows = computed(() => Object.values(pkgs.value))
const unusedRows = computed(() => pkgRows.value.filter(r => !r.in_use))
const sumMb = rows => Math.round(rows.reduce((s, r) => s + (r.size_mb || 0), 0))
const pkgTotalMb = computed(() => sumMb(pkgRows.value))
const unusedMb = computed(() => sumMb(unusedRows.value))
// 备份产物（exports/）：升级前快照 + 定时备份 + 手动导出，长期不回收会堆到几百 MB
const expRows = ref([]), expMsg = ref(''), pruneDays = ref(30)
const expTotalMb = computed(() => Math.round(expRows.value.reduce((s, r) => s + (r.size_mb || 0), 0)))
const expStaleRows = computed(() => expRows.value.filter(r => r.age_days >= pruneDays.value))
// 独立加载：失败不牵连主流程（旧服务端可能还没这个端点）
const loadPackages = () => listPackages()
  .then(p => (pkgs.value = Object.fromEntries((p || []).filter(x => x.exists).map(x => [x.dice, x]))))
  .catch(() => {})
const loadExports = () => listExports().then(e => (expRows.value = e || [])).catch(() => {})
const refreshCache = () => { loadPackages(); loadExports() }
loadPackages(); loadExports()           // 已登录的整页刷新场景直接拉；未登录由 WS onOpen 兜底
// ---------- 两栏布局：登录端在左、应用端在右 ----------
// 登录端程序 = 在任意应用端 manifest 的 compatible_login 里出现过的程序
// （纯清单驱动，与 Wizard 配对模式同口径，不写程序名分支）
const loginSet = computed(() => {
  const s = new Set()
  for (const m of Object.values(manifests.value))
    (m.compatible_login || []).forEach(o => { if (o !== 'builtin') s.add(o) })
  return s
})
const isLogin = n => loginSet.value.has(n.dice)
// 该登录端能否挂多个 QQ。只有多账号端（sealdice/snowluma）关联时才需要问
// 「绑哪个号」；单账号端（llbot/napcat/lagrange 系）选谁都只有一个结果，
// 那个下拉框是纯噪音，还会让人以为绑定方式有得选。
const loginMultiAccount = loginId => {
  const n = nodes.value.find(x => x.id === loginId)
  if (n) return !!n.multi_account          // ws_overview 直接推送该字段
  return !!manifests.value[n?.dice]?.multi_account   // 回退到清单
}
const needsAccountPick = loginId => loginMultiAccount(loginId)
const loginNodes = computed(() => nodes.value.filter(isLogin))
const diceNodes = computed(() => nodes.value.filter(n => !isLogin(n)))
// 侧栏打开时收窄画布（列距拉近、viewBox 变窄），节点/文字保持原大——
// 而不是让 SVG 随容器等比缩小（那样节点和字都会变小）
// 画布尺寸**恒定**，不随选中状态变化。
// 侧栏已改为常驻（宽度固定 340px），若这里还按 selected 切两套坐标，
// 选中/取消时节点会横向抖动一下 —— 看着像页面跳变，且用户会以为点空了。
// 窄容器下靠 CSS 缩放（.topo 的 width:100%）而不是改坐标。
const vbW = 800
const colX = { login: 170, dice: 630 }
const pos = id => {
  const node = nodes.value.find(n => n.id === id)
  if (!node) return { x: (colX.login + colX.dice) / 2, y: 215 }
  const col = isLogin(node) ? loginNodes.value : diceNodes.value
  const j = col.findIndex(n => n.id === id)
  const n = col.length
  const y = n <= 1 ? 215 : 60 + j * (310 / (n - 1))
  return { x: isLogin(node) ? colX.login : colX.dice, y }
}
// 分化 C3：面板重启按钮只在 server 版渲染（后端 /panel/restart 对 desktop 返 404）
const isServer = ref(false)
getEdition().then(e => { isServer.value = e === 'server' }).catch(() => {})

onMounted(() => {
  sock = connectWS('/ws/overview', m => {
    if (m.type !== 'overview') return
    nodes.value = m.payload.nodes; edges.value = m.payload.edges
    resmon.value = m.payload.resmon
  }, () => {
    connected.value = true
    refreshCache()                      // WS 连上=登录完成，此时才拉得到缓存/备份占用
  })
})
onUnmounted(() => sock?.close())

// WS 每 2s 全量替换 nodes，sel 里存的是点击时刻的快照（login_ref/qq 会过期）——
// 展示一律读 selLive（按 id 回当前节点），操作用 sel.id（稳定）
const selLive = computed(() => nodes.value.find(n => n.id === sel.value?.id) || sel.value)
const live = computed(() => selLive.value || {})
const linkTarget = computed(() =>
  live.value.login_ref ? nodes.value.find(n => n.id === live.value.login_ref) : null)
// 连线状态 → 中文文案（edge.state 的取值即样式类名，与 ws_overview 保持一致）
const stateText = s => ({ 'solid-green': '已连接', 'dashed-gray': '已配置未连接',
                          'solid-red': '连接失败' })[s] || s
// 可关联的登录端候选：manifest 的 compatible_login 声明 ∩ 当前存活节点
const linkCandidates = computed(() => {
  if (!sel.value) return []
  const opts = (manifests.value[sel.value.dice]?.compatible_login || [])
    .filter(o => o !== 'builtin')
  return nodes.value.filter(n => opts.includes(n.dice) && n.id !== sel.value.id)
})
watch(sel, () => {
  webuiInfo.value = null; connMsg.value = ''
  newLoginRef.value = ''; newAccountQq.value = ''
  // 管理面板必须收起：能力属于上一个实例，留着会让人对着 A 实例点 B 实例的操作
  manageOpen.value = false; manage.value = {}; manageErr.value = ''
  manageMsg.value = ''; depsData.value = null; pkgSpec.value = ''
  // 连接管理 / 定时任务 / 资源曲线同理：内容都绑在实例上，换实例必须一并收起
  connOpen.value = false; schedOpen.value = false
  showMetrics.value = false; metrics.value = null
})

// 启停操作：后端在启动/重启时会顺带开放 WebUI 端口（绑定修正 + ufw），有提示就展示
// 拓扑图方框下方的符号按钮：不经侧栏，直接对指定节点操作。
// sel 也要跟着切过去 —— 否则右侧详情与连线状态显示的还是上一个实例，
// 用户点完「停」看不到对应变化，会以为按钮没生效。
const quickOp = (id, o) => guard(async () => {
  sel.value = nodes.value.find(n => n.id === id)
  if (!sel.value) return
  const r = await opInstance(id, o)
  connMsg.value = r?.webui_note || ''
})
// 打开/收起管理面板。两个入口共用：
//  ① 拓扑图方框右侧的 ⚙（传节点对象）
//  ② 侧栏「管理应用」按钮（不传参数，用当前选中）
//
// 语义一（按用户要求）：程序**自带 WebUI** 时直接开新窗口去它那儿管理，
// 面板里那个「打开 WebUI」按钮就多余了 —— 多一次点击。
// 语义二：点开 → 再点收。所以要先记住「上一次开的是哪个实例」，
// 否则切到别的实例再点一下会误判成「收起」。
let manageOpenFor = null
const quickManage = (n = null) => guard(async () => {
  if (n) sel.value = n
  if (!sel.value) return
  if (manageOpen.value && manageOpenFor === sel.value.id) {
    manageOpen.value = false          // 再点一次：收起
    manageOpenFor = null
    return
  }
  manageOpen.value = true
  manageOpenFor = sel.value.id
  manageMsg.value = ''
  manageErr.value = ''
  const cap = await instanceManage(sel.value.id)   // 先取能力再决定去哪儿
  manage.value = cap
  depsData.value = depsDataOf()
  // 有 WebUI 能力 → 直接开新窗口，面板本身收起（用户要的是「去它那儿管理」）
  if ((cap?.capabilities || []).some(c => c.kind === 'webui')) {
    manageOpen.value = false
    manageOpenFor = null
    await openWebui()
  }
})
// 模板里不能直接用 location（会编译成 _ctx.location），一律包成方法
// 在文件管理器里打开实例目录：仅 desktop 有这个概念，server 是无人值守服务器，
// 按钮与后端端点**两处都要挡**（后端见 rest.py reveal_instance）——只挡一处会
// 出现「按钮在但点了 404」，用户只会以为面板坏了。
const openFolder = () => guard(async () => {
  connMsg.value = ''
  await revealInstance(sel.value.id)
  connMsg.value = `已在文件管理器中打开 ${selLive.value.dir || '实例目录'}`
})

const goLogs = () => { location.hash = `#/logs?instance=${sel.value.id}` }
const del = () => {
  // 是否建议保留存档目录由 manifest 声明（delete_keeps_save），不再按程序名硬编码
  const keeps = !!manifests.value[sel.value.dice]?.delete_keeps_save
  // 该实例作为登录端被哪些应用端引用：删除会级联解除它们的关联，提前告知
  const linkedBy = nodes.value.filter(n => n.login_ref === sel.value.id && n.id !== sel.value.id)
  const linkWarn = linkedBy.length
    ? `\n\n⚠ 它正被 ${linkedBy.length} 个实例关联（${linkedBy.map(n => n.dice).join('、')}），删除后将自动解除这些关联。`
    : ''
  if (!confirm(`确认删除 ${sel.value.dice} 实例 ${sel.value.id}？${linkWarn}`)) return
  const removeDir = confirm(keeps
    ? '同时删除程序文件夹？\n（该程序建议保留存档目录）'
    : '同时删除程序文件夹？')
  delInstance(sel.value.id, true, removeDir, removeDir && keeps)
    .then(r => {
      if (r?.unlinked?.length)
        connMsg.value = `已删除；并解除了 ${r.unlinked.length} 个实例与它的关联。`
      sel.value = null
    })
}

const guard = async fn => {              // 面板操作统一报错出口，失败不静默
  try { await fn() } catch (e) { connMsg.value = e.message || String(e) }
}

// ---------- 面板自管理：整体重启 ----------
const panelRestarting = ref(false)
const restartPanelNow = async () => {
  if (!confirm('整体重启管理面板？\n\n所有实例将随面板退出，重启完成后处于运行状态的实例自动拉回（约 5-10 秒）。')) return
  panelRestarting.value = true
  try {
    await restartPanel()
  } catch (e) {
    panelRestarting.value = false
    connMsg.value = e.message || String(e)
    return
  }
  // 轮询等面板换新完成：恢复应答后整页刷新拿新前端与全新 WS
  const deadline = Date.now() + 60000
  const poll = () => listManifests()
    .then(() => location.reload())
    .catch(() => {
      if (Date.now() >= deadline) {
        panelRestarting.value = false
        connMsg.value = '等待超时：面板未能自行恢复，请到服务器检查 dicemanager 服务状态。'
      } else setTimeout(poll, 2000)
    })
  setTimeout(poll, 3000)                 // 先给重启留 3 秒，再开始轮询
}

// ---------- 缓存 / 备份回收操作（与 Wizard 原实现同口径） ----------
const removePkgOf = dice_ => guard(async () => {
  pkgMsg2.value = ''
  await deletePackage(dice_)
  const next = { ...pkgs.value }; delete next[dice_]; pkgs.value = next
  pkgMsg2.value = `已删除 ${dice_} 的本地包，下次部署将在线下载。`
})
// 一键清理死缓存：没有任何实例在用的种子包（按需触发，不做自动删除）
const cleanUnused = () => guard(async () => {
  pkgMsg2.value = ''
  const r = await deleteUnusedPackages()
  const next = { ...pkgs.value }
  ;(r.removed || []).forEach(n => delete next[n])
  pkgs.value = next
  pkgMsg2.value = r.removed && r.removed.length
    ? `已清理 ${r.removed.join(' / ')}，释放约 ${r.freed_mb} MB。`
    : '没有需要清理的未使用缓存。'
})
const delExportFile = name => guard(async () => {
  expMsg.value = ''
  await deleteExport(name)
  expRows.value = expRows.value.filter(r => r.name !== name)
  expMsg.value = `已删除备份 ${name}。`
})
const pruneOldExports = () => guard(async () => {
  expMsg.value = ''
  const r = await pruneExports(pruneDays.value)
  const names = r.removed || []
  expRows.value = expRows.value.filter(x => !names.includes(x.name))
  expMsg.value = names.length
    ? `已清理 ${names.length} 份超过 ${pruneDays.value} 天的备份，释放约 ${r.freed_mb} MB。`
    : `没有超过 ${pruneDays.value} 天的备份。`
})

// ---------- 上传备份：停机 → 覆盖导入 → 自动重启（后端 /instances/{id}/backup） ----------
const backupInput = ref(null)
const uploading = ref(false), uploadJob = ref(null)   // 当前上传任务（可取消）
const pickBackup = () => backupInput.value?.click()
const doBackup = e => guard(async () => {
  const f = e.target.files?.[0]
  e.target.value = ''                    // 清空以便可重复选择同一文件
  if (!f || !sel.value) return
  if (!confirm(`把备份导入到 ${sel.value.dice} 实例 ${sel.value.id}？\n\n`
    + '· 运行中的实例会先停止，完成后自动重启\n'
    + '· 包内文件覆盖实例目录中的同名文件（包外文件不受影响）\n'
    + '· 支持 zip / tar.gz / tar.xz / tar.bz2 / tar，上限 2GB')) return
  connMsg.value = '上传中 0%'
  uploading.value = true
  uploadJob.value = uploadBackup(sel.value.id, f, p => {
    connMsg.value = p.sent ? '上传完成，服务端正在解压恢复…'
      : `上传中 ${p.total ? Math.round(p.loaded / p.total * 100) : 0}%`
  })
  uploadJob.value
    .then(r => {
      connMsg.value = `备份导入完成：${r.format} 格式，恢复 ${r.files} 个文件`
        + (r.restart_error ? `；⚠ 自动重启失败：${r.restart_error}（请手动点「启动」）`
           : r.restarted ? '，实例已重启' : '；实例原本未运行，保持停止')
    })
    .catch(e => { connMsg.value = e.message || String(e) })
    .finally(() => { uploading.value = false; uploadJob.value = null })
})
const cancelUpload = () => { uploadJob.value?.abort?.() }
// ---------- 安装根扫描：游离目录 / 游离进程 ----------
const scanRes = ref(null)
const scan = () => guard(async () => {
  connMsg.value = ''
  scanRes.value = await scanInstallRoots()
})
const delOrphan = path => guard(async () => {
  if (!confirm(`⚠ 确认删除游离目录 ${path}？\n此操作不可恢复，请确认其中没有需要保留的数据！`)) return
  await deleteOrphanDir(path)
  connMsg.value = `已删除 ${path}`
  scanRes.value = await scanInstallRoots()
})
const killProc = pid => guard(async () => {
  if (!confirm(`确认结束游离进程 ${pid}？`)) return
  await killOrphanProc(pid)
  connMsg.value = `已结束进程 ${pid}`
  scanRes.value = await scanInstallRoots()
})
// ---------- 管理应用（能力由后端适配器声明）----------
const manageOpen = ref(false), manageBusy = ref(false)
// 可折叠面板：连接管理 / 定时任务 / 资源曲线 / 管理应用 / 扫描结果。
// 语义统一为「点开 → 再点收」：按钮显示为按下态时表示面板正开着。
// 各面板互斥吗？不互斥（用户可能要同时看连接与诊断），
// 但**切换实例时必须全部收起** —— 面板内容属于上一个实例，
// 留着会让人对着 A 实例的操作按钮去点 B 实例的字段。
const connOpen = ref(false), schedOpen = ref(false), scanOpen = ref(false)
const togglePanel = which => {
  if (which === 'conn') { connOpen.value = !connOpen.value; return }
  if (which === 'sched') { schedOpen.value = !schedOpen.value; return }
}
const closeAllPanels = () => {
  connOpen.value = false; schedOpen.value = false
  manageOpen.value = false; showMetrics.value = false
}
const manage = ref({}), manageErr = ref(''), manageMsg = ref('')
const depsData = ref(null), pkgSpec = ref('')
const depsDataOf = () => manage.value?.data?.python_deps || null

const loadManage = () => guard(async () => {
  manageErr.value = ''
  manage.value = await instanceManage(sel.value.id)
  depsData.value = depsDataOf()
})

// 装/卸插件都可能跑几分钟（pip 装依赖），故用 manageBusy 独占按钮；
// 期间实例状态可能变，装完重新拉一次能力列表让 UI 与后端一致
const doInstallPkg = () => guard(async () => {
  const spec = pkgSpec.value.trim()
  if (!spec) return
  manageBusy.value = true
  manageErr.value = ''
  manageMsg.value = `正在安装 ${spec}（首次可能需要几分钟，取决于网络）…`
  try {
    const r = await installPackage(sel.value.id, spec)
    pkgSpec.value = ''
    depsData.value = r.data || depsDataOf()
    manageMsg.value = `已安装 ${spec} ${r.version || ''}。需重启实例后生效。`.trim()
  } catch (e) {
    manageErr.value = String(e.message || e)
  } finally {
    manageBusy.value = false
  }
})

const doUninstallPkg = async (name) => {
  if (!confirm(`确认卸载插件 ${name}？\n\n将同时删除 libs/ 下的包文件与 pyproject.toml 里的依赖声明。\n插件自身的数据文件（若有）不会删除。`))
    return
  guard(async () => {
    manageBusy.value = true
    manageErr.value = ''
    manageMsg.value = `正在卸载 ${name}…`
    try {
      const r = await uninstallPackage(sel.value.id, name)
      depsData.value = r.data || depsDataOf()
      manageMsg.value = `已卸载 ${name} ${r.version || ''}。需重启实例后生效。`.trim()
    } catch (e) {
      manageErr.value = String(e.message || e)
    } finally {
      manageBusy.value = false
    }
  })()
}

const doSyncPyproject = () => guard(async () => {
  manageBusy.value = true
  manageErr.value = ''
  manageMsg.value = '正在同步依赖声明…'
  try {
    const r = await syncPyproject(sel.value.id)
    depsData.value = r.data || depsDataOf()
    manageMsg.value = '已把 libs/ 中的包补写进 pyproject.toml。'
  } catch (e) {
    manageErr.value = String(e.message || e)
  } finally {
    manageBusy.value = false
  }
})

// WebUI：端口由后端回读（实际端口优先），URL 用面板同主机名拼接（服务与面板同机）
const openWebui = () => guard(async () => {
  const w = await instanceWebui(sel.value.id)
  if (!w.port) { connMsg.value = '未发现 WebUI 端口：实例未启动，或该程序没有 WebUI。'; return }
  webuiInfo.value = w
  window.open(`http://${location.hostname}:${w.port}`, '_blank', 'noopener')
})
const copyToken = () => {
  if (webuiInfo.value?.token) navigator.clipboard?.writeText(webuiInfo.value.token)
      .catch(() => {})
  connMsg.value = webuiInfo.value?.token ? '令牌已复制到剪贴板' : ''
}
// 连接管理（多连一 / 一连多）：以完整 links 数组替换关联；重写互联配置复用向导第 4 步。
// 地址与令牌留空 → 后端自动继承登录端各账号的端口与 token，保证两端一致。
const newLoginRef = ref(''), newAccountQq = ref('')
// 当前选中实例：是否登录端（展示账号视角而非关联管理视角）
const isLoginSel = computed(() => !!selLive.value && loginSet.value.has(selLive.value.dice))
// 应用端：当前关联列表（解析出对端节点、账号清单与连线状态），供面板逐条管理
const linkRows = computed(() => {
  if (!selLive.value || isLoginSel.value) return []
  const sid = sel.value?.id
  return (selLive.value.links || []).map(l => {
    const li = nodes.value.find(n => n.id === l.login_ref) || {}
    const st = edges.value.find(e => e.src === sid && e.dst === l.login_ref
                                  && (e.account_qq || '') === (l.account_qq || ''))
    return { key: `${l.login_ref}|${l.account_qq || ''}`, loginRef: l.login_ref,
             loginDice: li.dice || l.login_ref, accountQq: l.account_qq || '',
             accounts: li.accounts || [],
             state: st ? st.state : 'dashed-gray' }
  })
})
// 新增关联时，所选登录端的可绑定账号
const newLoginAccounts = computed(() => {
  const li = nodes.value.find(n => n.id === newLoginRef.value)
  return li?.accounts || []
})
// 登录端：被哪些应用端关联（用于展示「谁在用我」）
const linkedBy = computed(() => {
  if (!isLoginSel.value) return []
  return nodes.value.filter(n => (n.links || []).some(l => l.login_ref === sel.value.id))
})
const addLink = () => guard(async () => {
  if (!newLoginRef.value) return
  const links = (selLive.value.links || []).map(l => ({ ...l }))
  const nk = `${newLoginRef.value}|${newAccountQq.value || ''}`
  if (links.some(l => `${l.login_ref}|${l.account_qq || ''}` === nk)) {
    connMsg.value = '该关联已存在。'; return
  }
  links.push({ login_ref: newLoginRef.value, account_qq: newAccountQq.value || null })
  await linkInstance(sel.value.id, links)
  connMsg.value = '已关联' + (newAccountQq.value ? `（账号 ${newAccountQq.value}）` : '')
    + '。建议点「重写互联配置」自动对齐两端的地址与令牌。'
  newLoginRef.value = ''; newAccountQq.value = ''
})
const removeLink = (lr, qq) => guard(async () => {
  if (!confirm('解除该关联？（不删除任何实例，应用端对应连接将失效）')) return
  const links = (selLive.value.links || []).filter(
    l => !(l.login_ref === lr && (l.account_qq || '') === (qq || '')))
  await linkInstance(sel.value.id, links)
  connMsg.value = '已解除该关联。'
})
const updateLinkAccount = (lr, oldQq, newQq) => guard(async () => {
  const links = (selLive.value.links || []).map(l =>
    (l.login_ref === lr && (l.account_qq || '') === (oldQq || ''))
      ? { ...l, account_qq: newQq || null } : l)
  await linkInstance(sel.value.id, links)
  connMsg.value = '已更新绑定账号，建议点「重写互联配置」对齐连接。'
})
const unlinkAll = () => guard(async () => {
  if (!confirm('解除与全部登录端的关联？（不删除任何实例）')) return
  await linkInstance(sel.value.id, null)
  connMsg.value = '已解除全部关联。'
})
const reconn = () => guard(async () => {
  const r = await wizardStep(sel.value.id, 4, {})
  connMsg.value = r.result === 'ok' ? `互联配置已重写：${r.preview || ''}` : (r.message || '重写失败')
})

// ---------- 小操作：复制实例 ID（审查 #23） ----------
const copyId = () => {
  navigator.clipboard?.writeText(sel.value.id).catch(() => {})
  connMsg.value = `已复制实例 ID：${sel.value.id}`
}

// ---------- 备份导出（拓展1）：整目录 / 应用数据两种口径，语义必须分清 ----------
const doExport = scope => guard(async () => {
  const what = scope === 'full'
    ? '整目录备份（程序+配置+存档+数据，恢复即得完整实例）'
    : '数据备份（仅应用数据/存档，对应程序自身备份功能的局部数据；恢复前提是实例已部署同版本程序）'
  if (!confirm(`导出 ${sel.value.dice} 实例的${what}？\n\n`
    + '· 实例运行中导出时，数据文件可能正在写入，建议停止实例后导出\n'
    + '· 大实例打包可能耗时数十秒，请勿关闭页面')) return
  connMsg.value = '正在打包备份，请稍候…'
  const r = await exportBackup(sel.value.id, scope)
  connMsg.value = `备份已开始下载（${scope === 'full' ? '整目录' : '数据'}口径，${r.files} 个文件）。`
})

// ---------- 资源曲线（拓展5）：24h 内存 / CPU 走势 ----------
// 双线共用一张图：两条曲线各自按自己的峰值归一化（内存 MB 与 CPU% 量级不同，
// 强行共用一个 Y 轴会让 CPU 线贴底看不见），图例给出真实数值与峰值。
const showMetrics = ref(false), metrics = ref(null), metricsHours = ref(24)
const chartW = 320, chartH = 96
const chartPts = computed(() => metrics.value?.points || [])
const cpuMax = computed(() => {
  const v = chartPts.value.map(p => p[2])
  return v.length ? Math.max(...v, 5) : 0            // 低负载时给 5% 底，避免线贴底
})
const _path = idx => {
  const pts = chartPts.value
  if (pts.length < 2) return ''
  const vals = pts.map(p => p[idx])
  const max = Math.max(...vals, idx === 2 ? 5 : 1) || 1
  const t0 = pts[0][0], span = Math.max(pts[pts.length - 1][0] - t0, 1)
  // polyline 的 points 是「x,y x,y …」点序列（不是 path 的 M/L 命令）
  return pts.map(p => {
    const x = ((p[0] - t0) / span) * chartW
    const y = chartH - (p[idx] / max) * chartH
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}
const memPath = computed(() => _path(1))
const cpuPath = computed(() => _path(2))
const chartRange = computed(() => {
  const pts = chartPts.value
  if (!pts.length) return ''
  const hhmm = t => new Date(t * 1000).toTimeString().slice(0, 5)
  return `${hhmm(pts[0][0])} — ${hhmm(pts[pts.length - 1][0])} · ${pts.length} 个采样点`
})
const loadMetrics = () => guard(async () => {
  metrics.value = await instanceMetrics(sel.value.id, metricsHours.value)
})
const toggleMetrics = () => {
  if (showMetrics.value) { showMetrics.value = false; return }
  showMetrics.value = true
  loadMetrics()
}
// 资源曲线随实例切换收起（其余面板的收起已在上面那个 watch(sel) 里统一处理，
// 不在这里再写一遍 —— 同一件事分两处写必然漂移）

// ---------- 互联诊断（拓展2） ----------
const diag = ref(null)
const runDiagnose = () => guard(async () => {
  connMsg.value = ''
  diag.value = await diagnoseInstance(sel.value.id)
})

// ---------- 升级通道（拓展11） ----------
const upgradeLatest = ref('')
const checkUpgrade = () => guard(async () => {
  connMsg.value = ''
  const r = await upgradeCheck(sel.value.id)
  if (!r.supported) { connMsg.value = r.message; upgradeLatest.value = ''; return }
  if (r.up_to_date) { connMsg.value = `已是最新版本（${r.current}）`; upgradeLatest.value = ''; return }
  // pip_project 无 release tag（latest 为 null）：仍要给出升级入口，
  // 只是按钮文案从「升级到 vX」退化为「升级依赖」。
  upgradeLatest.value = r.latest || 'deps'
  connMsg.value = r.message
    || `发现新版本：${r.current || '(未知)'} → ${r.latest}。点「升级」开始（自动先整目录备份）。`
})
const doUpgrade = () => guard(async () => {
  const pipMode = upgradeLatest.value === 'deps'
  if (!confirm(`升级 ${sel.value.dice} 实例？\n\n`
    + '· 会自动先做整目录备份（升级失败可回退）\n'
    + (pipMode
      ? '· 运行中的实例先停止，用 pip install -U 升级全部依赖后自动重启\n'
      : '· 运行中的实例先停止，覆盖解压最新包（数据/存档保留）后自动重启\n')
    + '· 需从上游下载程序包，可能耗时数分钟')) return
  connMsg.value = pipMode
    ? '升级中（备份 → 停机 → pip install -U → 重启）…'
    : '升级中（备份 → 停机 → 覆盖新包 → 重启）…'
  const r = await upgradeInstance(sel.value.id)
  connMsg.value = `升级完成：${r.version || '新包已部署'}；升级前备份 ${r.backup}`
    + (r.restart_error ? `；⚠ 重启失败：${r.restart_error}` : r.restarted ? '，实例已重启' : '')
  upgradeLatest.value = ''
})

// ---------- 定时任务（拓展7） ----------
const schedules = ref([])
const newSched = ref({ kind: 'restart', scope: 'data', days: 1, hours: 0 })
const loadSchedules = () => listSchedules()
  .then(ts => (schedules.value = ts)).catch(() => {})
const addSched = () => guard(async () => {
  await addSchedule({ inst_id: sel.value.id, kind: newSched.value.kind,
                      every_days: newSched.value.days || 0,
                      every_hours: newSched.value.hours || 0,
                      scope: newSched.value.scope, keep: 7 })
  connMsg.value = `定时任务已添加（每 ${newSched.value.days || 0} 天 ${newSched.value.hours || 0} 小时自动执行一次）。`
  newSched.value.days = 1
  newSched.value.hours = 0
  await loadSchedules()
})
const delSched = s => guard(async () => {
  if (!confirm(`删除定时任务：${s.inst_id} 的${s.kind === 'restart' ? '定时重启' : '定时备份'}？`)) return
  await delSchedule(s.id)
  await loadSchedules()
})
const runSched = s => guard(async () => {
  await runSchedule(s.id)
  connMsg.value = '定时任务已执行完成（备份任务产出在服务器 exports/backups 目录）。'
})
watch(sel, () => { if (sel.value) { loadSchedules() } })
onMounted(() => { loadSchedules() })
</script>

<style scoped>
line.solid-green { stroke: #42b883; stroke-width: 3; }
line.dashed-gray { stroke: #bbb; stroke-dasharray: 6 4; }
line.solid-red   { stroke: #e5484d; stroke-width: 3; }
/* 连线中点状态标注：填充色与对应线一致，白描边（paint-order 让描边垫底）保证可读 */
text.edge-label { font-size: 11px; paint-order: stroke; stroke: var(--bg, #fff); stroke-width: 4px; }
text.edge-label.solid-green { fill: #2f9d6f; }
text.edge-label.dashed-gray { fill: #999; }
text.edge-label.solid-red   { fill: #e5484d; }
.legend { display: flex; gap: 18px; font-size: 12px; color: #666; margin: 2px 0 6px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.lg-line { display: inline-block; width: 22px; height: 0; border-top: 3px solid; }
.lg-green { border-color: #42b883; }
.lg-gray  { border-color: #bbb; border-top-style: dashed; }
.lg-red   { border-color: #e5484d; }
rect.dead { fill: var(--code-bg); opacity: .6; }
text.warn { fill: #d97706; font-size: 10px; }
/* 节点旁的符号按钮：靠 hover/禁用态区分可点性（图形没有文字标签，
   靠 <title> 兜 tooltip）。off = 该操作当前不适用（如已在运行还点「启动」）。
   选择器不带 .node-ops 前缀：管理应用那颗在 <g class="node-ops"> 之外。 */
/* 节点符号按钮的样式在 style.css（全局）—— SVG 元素拿不到 scoped 的 data-v 属性，
   放在这里会完全不生效，见 style.css 里那段注释。 */   /* 虚线圆环 = 齿轮的「齿」 */
.qbtn-side:hover { fill: var(--brand); }
.crash-warn { color: #e5484d; font-weight: 600; }
text.col-title { fill: #888; font-size: 15px; font-weight: 600; }
text.col-empty { fill: #bbb; font-size: 12px; }
.bar { width: 320px; height: 12px; background: var(--track); border-radius: 6px; }
.fill { height: 100%; background: #42b883; border-radius: 6px; }
.fill.alert { background: #e5484d; }
.scanbox {
  margin: 10px 0; padding: 10px 14px; max-width: 720px;
  border: 1px solid var(--border); border-radius: 8px; background: var(--panel-2);
}
.scan-row {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  padding: 3px 0; font-size: 13px; font-family: ui-monospace, Menlo, Consolas, monospace;
}
.kind-owned { color: var(--ok); }
.kind-orphan { color: #d97706; }
.kind-ext { color: #999; }
button.danger { color: #e5484d; }
/* 点击实例后：拓扑居左、操作面板固定宽度靠右成侧栏 */
.layout { display: flex; gap: 16px; align-items: flex-start; }
.topo-wrap { flex: 1; min-width: 0; }
/* 侧栏常驻，所以拓扑图宽度不再随选中状态跳变 —— 之前 aside 用 v-if，
   选中时挤进 340px、取消选中又弹回去，节点位置会在两套坐标间抖动。 */
.sel-empty {
  margin: 8px 0; padding: 14px 12px;
  border: 1px dashed var(--border); border-radius: var(--radius);
  text-align: center; line-height: 1.9;
}
.panel {
  width: 340px; flex-shrink: 0;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow);
  padding: 14px 16px;
}
.panel-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.panel-top .x {
  border: none; background: none; cursor: pointer;
  font-size: 18px; line-height: 1; color: var(--muted); padding: 2px 6px;
}
.panel-top .x:hover { color: var(--text); }
@media (max-width: 900px) {           /* 窄屏回退为上下堆叠 */
  .layout { flex-direction: column; }
  .panel { width: auto; }
}
.sel-info { margin-bottom: 8px; }
.iid { cursor: pointer; color: var(--muted); font-size: 12px; margin-left: 6px; }
.iid:hover { color: var(--brand); }
.diag { margin: 8px 0; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; }
.diag p { margin: 2px 0; font-size: 12.5px; }
.diag-ok { color: var(--ok); }
.diag-bad { color: var(--danger); }
.sched { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border); }
.sched-row { display: flex; gap: 8px; align-items: center; margin: 4px 0; font-size: 13px; flex-wrap: wrap; }
.sched-row span { flex: 1; min-width: 150px; }
.sched-row button { padding: 2px 10px; font-size: 12px; }
.sched-add { display: flex; gap: 8px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.sched-add select, .sched-add input { width: auto; margin: 0; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 6px 0; }
/* 连接管理：多连一 / 一连多 逐条关联卡片 */
.conn-mgmt { margin: 6px 0; border-top: 1px dashed var(--border); padding-top: 8px; }
.link-row { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
  margin-bottom: 8px; background: var(--code-bg); }
.link-main { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.add-link { border: 1px dashed var(--border); border-radius: 8px; padding: 8px 10px; margin-top: 6px; }
.link-state { font-size: 11px; padding: 1px 7px; border-radius: 999px; margin-left: 4px; }
.link-state.solid-green { color: #2f9d6f; background: #e7f6ee; }
.link-state.dashed-gray { color: #999; background: #f0f0f0; }
.link-state.solid-red { color: #e5484d; background: #fdeaea; }
.panel-ops { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.restart-overlay {
  position: fixed; inset: 0; z-index: 60;
  background: rgba(0, 0, 0, .35);
  display: flex; align-items: center; justify-content: center;
}
.restart-card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px 28px; text-align: center; font-size: 14px;
}
/* 缓存/备份管理（自 Wizard 移入） */
.cache-box { margin-top: 10px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; }
.cache-box summary { cursor: pointer; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.cache-box[open] summary { margin-bottom: 10px; }
.pkg { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; font-size: 13px; }
.pkg-ok { color: var(--ok); }
.pkg-idle { color: var(--muted); }
.inline { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.tok { cursor: pointer; background: var(--code-bg); padding: 2px 6px; border-radius: 4px; }
/* 资源曲线：两条线各自归一化，颜色语义与图例一致 */
.metrics { margin: 8px 0; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; }
.metrics-top { display: flex; gap: 10px; align-items: center; font-size: 13px; }
.metrics-top button { padding: 2px 10px; font-size: 12px; }
.chart { width: 100%; height: 96px; background: var(--code-bg); border-radius: 6px; }
.line-mem { stroke: #4f7cff; stroke-width: 2; border-color: #4f7cff; }
.line-cpu { stroke: #42b883; stroke-width: 2; border-color: #42b883; }
.metrics-legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: #666; margin-top: 4px; }
.metrics-legend span { display: inline-flex; align-items: center; gap: 6px; }

/* ---------- 管理应用浮窗 ----------
   为什么改成浮窗而不是侧栏内展开：侧栏只有 340px 宽，插件列表 + 安装表单
   挤在里面要滚好几屏，而且把下方的按钮全顶出视口。 */
.mask {
  position: fixed; inset: 0; z-index: 60;
  background: rgba(0,0,0,.42);
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
}
.manage-box {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius);
  width: min(680px, 100%); max-height: 86vh; overflow-y: auto;
  padding: 16px 18px;
  box-shadow: 0 12px 32px rgba(0,0,0,.22);
}
.manage-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-bottom: 10px;
  padding-bottom: 10px; border-bottom: 1px solid var(--border);
}
/* 面板按钮按下态：一眼看出「这个面板正开着」，再点即收起 */
.ops button.on { background: var(--brand); color: #fff; border-color: var(--brand); }
/* 扫描结果的折叠开关：它常常几十条，压在侧栏上方会把操作按钮顶出视口 */
.scan-toggle { margin: 4px 0; }
</style>
