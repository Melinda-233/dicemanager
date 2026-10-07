<template>
  <div class="wizard">
    <div class="wz-head">
      <h3>创建 — 第 {{ curStepIdx }}/{{ stepNames.length }} 步</h3>
      <ol class="steps">
        <li v-for="(s, i) in stepNames" :key="s" :class="{ done: curStepIdx > i + 1,
                                                      now: curStepIdx === i + 1 }">{{ s }}</li>
      </ol>
    </div>

    <!-- 断点续跑入口：管理器重启/断网留下的中间态实例 -->
    <div v-if="pending.length && step === 1 && !loading" class="pending">
      <p class="hint">有 {{ pending.length }} 个未完成的实例，可从中断处继续：</p>
      <div v-for="p in pending" :key="p.id" class="pending-item">
        <span>{{ p.dice }} · {{ p.id }}{{ p.qq ? ' (QQ ' + p.qq + ')' : '' }}
          — 下一步：{{ resumeLabel(p.next_step) }}</span>
        <button class="primary" :disabled="busy" @click="resume(p)">继续</button>
        <button class="danger" :disabled="busy" @click="stopPending(p)">停止并删除</button>
      </div>
    </div>

    <p v-if="loading" class="hint">正在加载程序清单…</p>
    <div v-else-if="loadError" class="empty">
      <p>程序清单加载失败：{{ loadError }}</p>
      <button class="primary" @click="load">重试</button>
    </div>
    <p v-else-if="err" class="err">{{ err }}</p>

    <!-- ============ 步骤一：选端类型 → 选程序 ============ -->
    <div v-if="step === 1" class="wz-body">
      <!-- 「创建并连接」进入的配对态：端类型已由来源端决定，程序候选已按兼容性收窄 -->
      <div class="pair-banner" v-if="pairFrom">
        <b>正在为 {{ pairFrom.dice }} 创建{{ role === 'login' ? '登录端' : '应用端' }}</b>
        <span class="hint">下面只列出与 {{ pairFrom.dice }} 兼容的程序；创建完成后将自动互联。</span>
        <button @click="cancelPair">取消</button>
      </div>
      <div class="field" v-else>
        <label>要创建哪一端</label>
        <div class="role-pick">
          <button v-for="r in ROLES" :key="r.id" type="button"
                  class="role-card" :class="{ on: role === r.id }"
                  :aria-pressed="role === r.id" @click="pickRole(r.id)">
            <b>{{ r.label }}</b>
            <span>{{ r.desc }}</span>
          </button>
        </div>
      </div>

      <div class="field" v-if="role">
        <label>{{ role === 'login' ? '登录端程序' : '应用端程序' }}</label>
        <select v-model="dice">
          <option v-for="n in rolePrograms" :key="n" :value="n">
            {{ n }}（{{ metaOf(n).arch === 'allinone' ? '整合包' : '独立程序'
            }}{{ metaOf(n).approx_memory_mb ? ' · 约 ' + metaOf(n).approx_memory_mb + ' MB' : '' }}）</option>
        </select>
        <p class="hint none" v-if="!rolePrograms.length">没有与 {{ pairFrom?.dice || '' }} 兼容的程序。</p>
        <p class="hint" v-else>{{ role === 'login' ? ROLE_DESC_LOGIN : ROLE_DESC_APP }}</p>
      </div>

      <!-- 接入通道：程序声明了多种通道时才出现（目前仅海豹支持官方机器人） -->
      <div class="field" v-if="role === 'app' && botModes.length > 1">
        <label>接入通道</label>
        <select v-model="botMode">
          <option v-for="b in botModes" :key="b.id" :value="b.id">{{ b.label }}</option>
        </select>
        <p class="hint" v-if="officialMode">
          官方通道由 {{ dice }} 自己对接 QQ 官方服务：<b>不需要登录端</b>，面板也不会写入
          OneBot 互联配置。启动后进 WebUI「添加账号 → 平台 QQ → QQ 官方机器人」，
          用 AppID + AppSecret 或扫码完成接入（需在开放平台把本机公网 IP 填进白名单）。
          <a v-if="officialGuide" :href="officialGuide" target="_blank" rel="noreferrer">官方接入手册</a>
        </p>
      </div>

      <!-- LLBot v8 的 AUTH TOKEN 提前到第一步：选完程序立刻填，扫码页免操作直接出码 -->
      <div class="field" v-if="needAuthToken">
        <label>AUTH TOKEN（LLBot v8.0.9+ 必填）</label>
        <input v-model="cred.auth_token" placeholder="AUTH TOKEN"/>
        <p class="hint">到 https://auth.luckylia.com 申请获取；进入扫码页时自动保存生效。</p>
      </div>

      <div class="field" v-if="dice">
        <label>离线程序包（可选）</label>
        <div class="pkg" v-if="pkgInfo">
          <span class="pkg-ok">✓ 本地已有：{{ pkgInfo.size_mb }} MB ·
            {{ pkgInfo.source === 'upload' ? '上传' : '下载缓存' }}于 {{ pkgInfo.updated_at }}</span>
          <button :disabled="pkgBusy" @click="removePkg">删除</button>
        </div>
        <p v-else class="hint">未上传：部署时将从 GitHub 在线下载（国内可能很慢）。</p>
        <p class="hint">上传 zip / tar.gz / tar.xz 等压缩包后，部署将直接解压本地包，不再联网下载；也可用同样的包供多实例复用。</p>
        <p class="hint" v-if="manifest.release_page && !pkgInfo">
          没有现成包？可到
          <a :href="manifest.release_page" target="_blank" rel="noreferrer">上游项目页</a>
          下载对应平台的压缩包后上传。
          <span v-if="manifest.prerequisite">（本程序还需先备好：{{ manifest.prerequisite }}）</span>
        </p>
        <input type="file" accept=".zip,.gz,.tgz,.xz,.bz2,.tar" :disabled="pkgBusy" @change="uploadPkg"/>
        <button v-if="pkgUp" class="danger" @click="cancelPkg">取消上传</button>
        <div v-if="pkgUp" class="pkgup">
          <div class="pkgup-bar" :class="{ 'is-indet': !pkgUp.total && !pkgUp.sent, 'is-sent': pkgUp.sent }">
            <i :style="{ width: pkgPercent + '%' }"></i>
          </div>
          <p class="hint">{{ pkgText }}</p>
        </div>
        <p v-if="pkgMsg" class="hint">{{ pkgMsg }}</p>
      </div>
      <!-- 下载前探测：把「将要下什么、多大、哪个版本」摆到点下载之前。
           上游改资产名会让部署直接 404（llbot v8.3.0 实测），而用户此前只能
           看到「部署失败」，分不清是包改名、网络问题，还是本来就要下 90MB。 -->
      <div class="field" v-if="dice && !pkgInfo">
        <label>将下载的程序包</label>
        <div class="pkgprobe" v-if="probing">
          <span class="hint">正在查询上游最新包…</span>
        </div>
        <div class="pkgprobe" v-else-if="pkgProbe">
          <p class="pkg-ok" v-if="pkgProbe.ok">
            {{ pkgProbe.asset }}<span v-if="pkgProbe.size"> · 约 {{ fmtMB(pkgProbe.size) }} MB</span>
            <span v-if="pkgProbe.tag"> · {{ pkgProbe.tag }}</span>
          </p>
          <p class="hint err" v-else>{{ pkgProbe.error }}</p>
          <p class="hint">启动文件：<code>{{ pkgProbe.executable }}</code>
            <span v-if="!pkgProbe.size">（大小未知，源站未提供 Content-Length）</span></p>
        </div>
        <p class="hint" v-else>—</p>
        <div class="ops">
          <button :disabled="probing" @click="loadProbe(true)">重新查询</button>
          <span class="hint" v-if="pkgProbe && !pkgProbe.ok">
            若提示找不到资产，通常是上游改了发布包名，可改为「上传离线程序包」部署。
          </span>
        </div>
      </div>

      <p v-if="manifest.prerequisite" class="hint">前置依赖：{{ manifest.prerequisite }}</p>
      <!-- 上游侧已知问题：提前告知，而不是让人部署跑完才看到「缺必备文件」。
           典型如 napcat——上游已停发 Linux 包，包能下但里面全是 Windows 文件。 -->
      <p v-if="manifest.known_issue" class="known-issue">
        ⚠ 已知问题：{{ manifest.known_issue }}
        <a v-if="manifest.release_page" :href="manifest.release_page"
           target="_blank" rel="noreferrer">查看项目页</a>
      </p>
      <div class="ops">
        <button class="primary" :disabled="!canCreate || busy" @click="create">
          下一步：{{ createLabel }}</button>
      </div>
    </div>

    <!-- ============ 步骤二：下载 / 解压部署 ============ -->
    <div v-else-if="step === 2" class="wz-body">
      <!-- 下载进度条：done/total 来自后端 /deploy-progress 轮询（每 1s）。
           总量未知（无 Content-Length / 假响应）时退化为不确定态动画。
           文字与进度条同时给：条给「到哪了」，字给「正在干什么」。 -->
      <div v-if="busy" class="dlprog">
        <div class="dlprog-bar" :class="{ 'is-indet': !dlPercent }">
          <i :style="{ width: (dlPercent || 100) + '%' }"></i>
        </div>
        <p class="hint">{{ deployMsg || (pkgInfo ? '正在解压本地程序包部署 ' + dice + '，请稍候…'
                                 : '正在下载部署 ' + dice + '，请稍候（首次可能耗时数分钟）…') }}</p>
        <p v-if="dlSpeed" class="hint">{{ dlSpeed }} · 已用 {{ dlElapsed }}</p>
      </div>
      <p v-else class="hint">部署中，请勿关闭页面…</p>
      <!-- 部署失败（下载超时/断网/上游改名等）：就地给出出路，不必退回第一步。
           「去项目页下载」是第三条路：在线下载失败时用户最需要的就是
           「我自己去 release 页把包下下来，再上传」——没有链接就只能干等或放弃。 -->
      <div v-if="deployFail" class="dialog deploy-fail">
        <p class="fail-msg">部署失败：{{ deployFail }}</p>
        <p class="hint">在线下载失败多为网络问题（国内直连 GitHub 常超时），
          也可能是上游改了发布包名。你可以：</p>
        <div class="ops">
          <a class="btn" v-if="manifest.release_page" :href="manifest.release_page"
             target="_blank" rel="noreferrer">去项目页下载</a>
          <span class="hint" v-if="manifest.release_page">
            打开后找对应平台的压缩包，下载完回到第一步「上传离线程序包」即可部署。</span>
        </div>
        <div class="ops">
          <button class="primary" :disabled="busy" @click="retryDeploy">重试下载</button>
          <label class="upload-btn" :class="{ disabled: pkgBusy || busy }">
            上传压缩包并部署
            <input type="file" accept=".zip,.gz,.tgz,.xz,.bz2,.tar"
                   :disabled="pkgBusy || busy" @change="uploadThenDeploy"/>
          </label>
        </div>
        <div v-if="pkgUp" class="pkgup">
          <div class="pkgup-bar" :class="{ 'is-indet': !pkgUp.total && !pkgUp.sent, 'is-sent': pkgUp.sent }">
            <i :style="{ width: pkgPercent + '%' }"></i>
          </div>
          <p class="hint">{{ pkgText }}</p>
        </div>
        <p v-if="pkgMsg" class="hint">{{ pkgMsg }}</p>
      </div>
      <div v-if="conflict" class="dialog">
        <p>同名文件夹已存在：<code>{{ conflictDir }}</code></p>
        <div class="ops">
          <button @click="resolve(true)">直接使用（校验必备文件）</button>
          <button @click="resolve(false)">新建序号文件夹</button>
        </div>
      </div>
    </div>

    <!-- ============ 步骤三：登录（登录端）+ 链接已有程序，随后自动启动 ============ -->
    <div v-else-if="step === 3" class="wz-body">
      <!-- 官方通道：连接动作在程序自身 WebUI 完成，这里把前置清单一次说清 -->
      <div v-if="officialMode" class="official-tip">
        <b>官方机器人通道 · 接入清单</b>
        <ol>
          <li>到 <a href="https://q.qq.com" target="_blank" rel="noreferrer">QQ 开放平台</a>
            实名创建机器人应用，取到 <code>AppID</code> 与 <code>AppSecret</code>。</li>
          <li>「开发设置 → IP 白名单」填本机<b>公网</b> IP（云服务器填控制台显示的 IP）。</li>
          <li>启动后打开下方 WebUI：添加账号 → 平台选「QQ」→ 连接方式选「QQ 官方机器人」，
            用 AppID + AppSecret 或扫码接入。</li>
          <li>指令须在开放平台配置（默认前缀 <code>/</code>），使用场景勾「QQ 群 / 频道私信 / QQ 频道」，
            <b>不要勾「消息列表」</b>。</li>
          <li>提交审核（自测报告 + 隐私协议）通过后「上线机器人」；先用沙盒群自测。</li>
        </ol>
        <p class="hint">优点：不走协议端，无风控封号风险。代价：官方接口能力受限，
          依赖主动消息、群管、本地媒体的功能可能不可用。</p>
      </div>

      <!-- 登录区：仅登录端与少数自带登录的应用端出现 -->
      <div v-if="needLoginHere && !loggedIn" class="login-box">
        <h4>登录 {{ dice }}</h4>

        <!-- 登录方式二选一：程序同时支持扫码与账密时才出现（能力由后端 login_modes 声明）。
             不做「一律都支持」——字段写错时程序会静默退回扫码，用户以为设了密码
             其实每次都在扫码，而扫码的滑块/风控成本远高于账密登录。 -->
        <div class="field" v-if="canPasswordLogin">
          <label>登录方式</label>
          <div class="mode-pick">
            <button type="button" class="mode-btn" :class="{ on: loginMode === 'qrcode' }"
                    @click="switchLoginMode('qrcode')">扫码登录</button>
            <button type="button" class="mode-btn" :class="{ on: loginMode === 'account' }"
                    @click="switchLoginMode('account')">账号密码登录</button>
          </div>
          <p class="hint">{{ loginMode === 'account'
            ? '密码会写入程序自己的配置文件，之后启动免扫码直接登录。'
            : '每次登录都需扫码，适合不方便输入密码的场合。' }}</p>
        </div>

        <!-- 扫码 -->
        <div v-if="showQrcode" class="qr">
          <img v-if="qr.url" :src="qr.url" alt="二维码"/>
          <img v-if="qr.base64" :src="qr.base64" alt="二维码"/>
          <div class="ops">
            <button @click="sock?.send('refresh')">刷新二维码</button>
            <button class="primary" @click="doLogin">
              {{ needAuthToken && !tokenSaved ? '保存TOKEN 并获取二维码' : '我已完成扫码' }}</button>
          </div>
          <a v-if="verifyUrl" :href="verifyUrl" target="_blank" rel="noreferrer">
            需要滑块验证：请手动完成（只转发不代做）</a>
        </div>

        <!-- 账号密码 -->
        <div v-else-if="loginMode === 'account'" class="field">
          <label>QQ 账号</label><input v-model="cred.qq" placeholder="QQ 账号"/>
          <label>密码</label><input v-model="cred.password" type="password" placeholder="密码"/>
          <label v-if="protocols.length">登录协议</label>
          <select v-if="protocols.length" v-model="cred.protocol">
            <option v-for="p in protocols" :key="p.id" :value="p.id">{{ p.label }}</option>
          </select>
          <p class="hint risk-note">⚠ 密码将<b>明文写入程序配置目录</b>下的配置文件
            （协议端本身即如此实现，面板无法代为加密）——服务器上任何能读该目录的人都拿得到。
            建议优先用扫码；密码登录仅限你有把握的受控环境。</p>
          <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">提交登录</button></div>
        </div>
        <!-- 登录端仅支持账密（login_modes 只有 account）时也走这个分支 -->
        <div v-else-if="loginType === 'account'" class="field">
          <label>QQ 账号</label><input v-model="cred.qq" placeholder="QQ 账号"/>
          <label>密码</label><input v-model="cred.password" type="password" placeholder="密码"/>
          <label v-if="protocols.length">登录协议</label>
          <select v-if="protocols.length" v-model="cred.protocol">
            <option v-for="p in protocols" :key="p.id" :value="p.id">{{ p.label }}</option>
          </select>
          <p class="hint risk-note">⚠ 密码将<b>明文写入程序配置目录</b>下的配置文件。</p>
          <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">提交登录</button></div>
        </div>

        <div v-else-if="loginType === 'webui'" class="field">
          <p class="hint">该程序需在其自带 WebUI 完成登录，dicemanager 不代劳：</p>
          <pre class="preview" v-if="manual">{{ manual }}</pre>
          <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">我已了解，继续</button></div>
        </div>
        <div v-else class="field">
          <p class="hint">正在准备登录…</p>
        </div>
        <div class="field" v-if="needAuthToken && !tokenSaved">
          <label>AUTH TOKEN（必填）</label>
          <input v-model="cred.auth_token" placeholder="AUTH TOKEN（LLBot v8.0.9+ 需申请）"/>
          <p class="hint">到 https://auth.luckylia.com 申请获取。第一步已填则自动保存，此处仅作补填兜底。</p>
        </div>
        <p class="hint" v-if="needAuthToken && tokenSaved">TOKEN 已保存，二维码生成中；扫码后点击「我已完成扫码」继续。</p>

        <!-- 登录日志（可选）：扫码/账密都停在「看不出进展」时，程序日志是唯一线索
             （二维码字符画、风控提示、协议握手失败都在里面）。
             复用总览页的 /ws/logs 通道，不新造后端端点。
             默认收起 —— 多数用户不需要，不该让日志占满登录区。 -->
        <div class="login-logs">
          <div class="ll-head">
            <label class="pick">
              <input type="checkbox" v-model="showLoginLog"/>
              <span>显示登录日志</span>
            </label>
            <span class="hint" v-if="showLoginLog">{{ logLines.length }} 行</span>
            <div class="ops" v-if="showLoginLog">
              <button @click="clearLoginLog">清空</button>
              <label class="pick">
                <input type="checkbox" v-model="logFollow"/>
                <span>自动滚动</span>
              </label>
            </div>
          </div>
          <div v-if="showLoginLog" class="term logbox" ref="logBox">
            <p v-if="!logLines.length" class="hint">
              暂无日志。若程序未在运行，日志会在向导启动实例后陆续出现。
            </p>
            <p v-for="(l, i) in logLines" :key="i" :class="['log-line', { 'log-err': l.error }]">{{ l.text }}</p>
          </div>
          <p v-else class="hint">
            登录卡住或报「已完成扫码却没反应」时，勾开日志能看到程序真实输出。
          </p>
        </div>
      </div>
      <p v-else-if="loggedIn" class="ok-line">✓ {{ dice }} 登录完成（<span v-if="curQQ">QQ {{ curQQ }} · </span>令牌与互联地址已就绪{{ loginMode === 'account' ? ' · 密码已保存，下次启动免扫码' : '' }}）</p>

      <!-- 链接区：登录端反向关联已有应用端 / 应用端关联已有登录端 -->
      <div v-if="!officialMode" class="link-box">
        <h4>{{ role === 'login' ? '链接管理器内已有的应用端' : '链接登录端' }}</h4>

        <!-- 登录端：勾选管理器内已存在且兼容的应用端，一次写入关联并重写其互联配置 -->
        <template v-if="role === 'login'">
          <p class="hint">勾选后会自动为这些应用端建立指向本登录端的关联，并重写它们的互联配置
            （一个登录端可同时供多个应用端使用）。</p>
          <div v-if="!appCandidates.length" class="hint none">暂无已创建且与 {{ dice }} 兼容的应用端。</div>
          <div v-for="a in appCandidates" :key="a.id" class="pick-row">
            <label class="pick">
              <input type="checkbox" :value="a.id" v-model="pickedApps"/>
              <span>{{ a.dice }} · {{ a.id }}{{ a.qq ? ' (QQ ' + a.qq + ')' : '' }}</span>
            </label>
            <select v-if="pickedApps.includes(a.id)" v-model="pickAcct[a.id]" class="acct">
              <option value="">默认账号（自动分配）</option>
              <option v-for="ac in myAccounts" :key="ac.qq" :value="String(ac.qq)">
                QQ {{ ac.qq }}{{ ac.status ? '（' + ac.status + '）' : '' }}</option>
            </select>
          </div>

          <!-- 创建并连接：跳回第一步新建一端，创建后自动互联（省掉手工去总览关联） -->
          <div class="ops">
            <button :disabled="busy || !compatibleApps.length" @click="gotoCreate('app')">
              ＋ 创建并连接应用端</button>
            <span class="hint" v-if="!compatibleApps.length">没有与 {{ dice }} 兼容的应用端程序。</span>
            <span class="hint" v-else>将新建一个 {{ compatibleApps.join(' / ') }} 实例并自动关联本登录端。</span>
          </div>
        </template>

        <!-- 应用端：关联一个或多个已有登录端，可按账号分发 -->
        <template v-else>
          <p class="hint" v-if="loginCandidates.length">每个关联生成一条连接；一个应用端可连多个登录端。</p>
          <div v-if="!loginCandidates.length" class="hint none">
            当前还没有已创建的登录端实例。
          </div>
          <div v-if="loginCandidates.length">
            <div class="field">
              <label>添加登录端</label>
              <select v-model="addLoginRef">
                <option value="" disabled>请选择</option>
                <option v-for="o in addableLogins" :key="o.id" :value="o.id">
                  {{ o.dice }} · {{ o.id }}{{ o.qq ? ' (QQ ' + o.qq + ')' : '' }}</option>
              </select>
              <div class="ops"><button :disabled="!addLoginRef || busy" @click="addLink">添加</button></div>
            </div>
            <p class="hint" v-if="loginOptions.length">兼容：{{ loginOptions.join(' / ') }}</p>
          </div>

          <!-- 创建并连接：跳回第一步新建登录端，创建后自动互联 -->
          <div class="ops">
            <button :disabled="busy || !loginOptions.length" @click="gotoCreate('login')">
              ＋ 创建并连接登录端</button>
            <span class="hint" v-if="!loginOptions.length">该应用端不依赖协议登录端（自带接入）。</span>
            <span class="hint" v-else>将新建一个 {{ loginOptions.join(' / ') }} 实例并自动关联本应用端。</span>
          </div>

          <div v-for="l in pickedLinks" :key="l.login_ref + '|' + (l.account_qq || '')" class="conn-peer">
            <div class="peer-head">
              <b>{{ loginInstOf(l.login_ref)?.dice || l.login_ref }}</b> · {{ l.login_ref }}
              <span v-if="l.account_qq">（账号 {{ l.account_qq }}）</span>
              <button class="lnk" @click="removeLink(l)">移除</button>
            </div>
            <div class="peer-grid">
              <div>
                <label>绑定账号</label>
                <select v-model="l.account_qq">
                  <option value="">默认账号（自动分配）</option>
                  <option v-for="a in accountsOf(l.login_ref)" :key="a.qq" :value="String(a.qq)">
                    QQ {{ a.qq }}{{ a.status ? '（' + a.status + '）' : '' }}</option>
                </select>
              </div>
              <div>
                <label>WS 模式</label>
                <select v-model="l.conn_direction">
                  <option value="forward">正向 WS（推荐：两端都选正向即可互通）</option>
                  <option value="reverse">反向 WS</option>
                </select>
              </div>
            </div>
          </div>
          <p class="hint" v-if="needsLoginEnd && !officialMode">
            尚未关联任何登录端：本应用端写入互联配置后会因无人监听而持续连接失败。可先继续，稍后在总览关联并「重写互联配置」。
          </p>
        </template>

        <details class="adv-box">
          <summary>高级：互联 Token（默认自动生成，一般无需修改）</summary>
          <div class="field">
            <label>互联 Token（两端必须一致，留空自动生成/沿用）</label>
            <input v-model="conn.token" placeholder="留空自动生成 / 沿用登录端的"/>
          </div>
        </details>

        <pre class="preview" v-if="preview">{{ preview }}</pre>
        <p v-if="manual" class="warn">{{ manual }}</p>
      </div>

      <div class="ops">
        <button class="primary" :disabled="busy || (!loggedIn && needLoginHere)" @click="finish">
          完成并启动</button>
        <span class="hint" v-if="busy">正在处理…</span>
      </div>
    </div>

    <!-- ============ 步骤四：已启动 ============ -->
    <div v-else-if="step === 4" class="wz-body">
      <div class="done-card">
        <b>已启动</b>
        <span class="hint">{{ dice }} · <code>{{ instanceId }}</code>{{ curQQ ? ' · QQ ' + curQQ : '' }}
          —— 总览图已出现新节点</span>
      </div>
      <div v-if="webuiInfo && webuiUrl" class="start-info">
        <a class="webui-link" :href="webuiUrl" target="_blank" rel="noreferrer">
          打开 WebUI（{{ webuiUrl }}）</a>
        <p v-if="webuiInfo.token" class="hint">
          WebUI 令牌：<code class="tok">{{ webuiInfo.token }}</code>（登录页粘贴使用）</p>
        <p v-else class="hint">令牌将从启动日志中回读，稍后可在总览查看。</p>
      </div>
      <div class="ops">
        <button class="primary" @click="goOverview">完成，回到总览</button>
        <button @click="reset">再建一个</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, reactive, watch, nextTick, onUnmounted } from 'vue'
import { connectWS } from '../ws'
import { listManifests, listInstances, listPending, listPackages, uploadPackage,
         deletePackage, linkInstance, probeManifestPackage,
         createInstance, wizardStep, delInstance, deployProgress, instanceWebui } from '../api'

const ROLES = [
  { id: 'login', label: '登录端', desc: '登录 QQ，向应用端提供协议接入' },
  { id: 'app',   label: '应用端', desc: '跑骰子 / 机器人框架，连接登录端取消息' },
]
const ROLE_DESC_LOGIN = '登录端负责登录 QQ 并对外提供协议接入，一个登录端可同时供多个应用端使用。'
const ROLE_DESC_APP = '应用端负责处理消息，需要连接一个登录端；也可先创建、稍后在总览页关联。'

const step = ref(0), manifests = ref({}), instances = ref([])
const role = ref('app'), dice = ref('')      // role: 'login' | 'app'（步骤一选端）
const loading = ref(true), loadError = ref(''), busy = ref(false), err = ref('')
const conflict = ref(false), conflictDir = ref(''), qr = ref({}), verifyUrl = ref('')
const cred = ref({ qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' })
const tokenSaved = ref(false)   // AUTH TOKEN 已落盘并重启过进程（LLBot v8 扫码前置条件）
const conn = ref({ direction: 'forward', addr: '', token: '' })
const preview = ref(''), manual = ref('')
const pending = ref([])          // 中间态实例（断点续跑入口）
const loggedIn = ref(false)      // 第三步：登录环节已完成（可进入链接/启动）
const webuiInfo = ref(null)
const pkgs = ref({})             // {dice: {exists,size_mb,source,updated_at}}
const pkgBusy = ref(false), pkgMsg = ref('')

// ---------- 下载前探测（将要下哪个包 / 多大 / 什么版本）----------
// 为什么要它：上游改资产名会让部署直接 404（llbot v8.3.0 把 LLBot-CLI-linux-x64.zip
// 改成 LuckyLillia-CLI-linux-x64.zip），滚动 tag 的资产名还每次都变
// （Dice-Next 带日期后缀）。此前用户只能看到「部署失败」，分不清是包改名、
// 网络问题，还是本来就要下 90MB —— 「点了才发现下错」太晚。
const pkgProbe = ref(null), probing = ref(false)
// 用 watch 而不是改 6 处 dice 赋值点：那些点分散在选端/选程序/配对/回退等分支，
// 逐个加必然漏一处（漏了就是「换个入口进来不探测」的行为不一致）。
watch(dice, () => loadProbe(), { immediate: false })
// 换程序要清掉上一次的探测结果，否则会显示上一个程序的包（很容易误读）
const loadProbe = async (force) => {
  const d = dice.value
  if (!d) { pkgProbe.value = null; return }
  // 本地已有包 → 部署走解压不下载，没什么可探的
  if (pkgs.value[d] && !force) { pkgProbe.value = null; return }
  probing.value = true
  pkgProbe.value = null
  try {
    const r = await probeManifestPackage(d)
    // 竞态：查询期间用户可能已经换程序了，丢弃过期结果
    if (dice.value !== d) return
    pkgProbe.value = { ...r, ok: !r.error }
  } catch (e) {
    if (dice.value === d) pkgProbe.value = { ok: false, error: e.message || String(e) }
  } finally {
    if (dice.value === d) probing.value = false
  }
}
// 登录端第三步：勾选要关联的已有应用端 + 每个应用端绑定的账号
const pickedApps = ref([])
const pickAcct = reactive({})
// 「创建并连接」：从第三步跳回第一步新建对端的一端，创建后自动互联。
// 这里只记「从谁跳过来的、跳去建哪一端」，其余复用第一步的选程序流程。
const pairFrom = ref(null)          // {id, dice, qq, target: 'app'|'login'}
// 应用端第三步：本实例的关联列表（每项 {login_ref, account_qq, conn_direction}）
const pickedLinks = ref([])
const addLoginRef = ref('')
// 登录方式：qrcode / account（能力由后端 login_modes 声明，不按程序名分支）
const loginMode = ref('qrcode')
// 独立加载：失败不牵连主流程（旧服务端可能还没这个端点）
// sock 必须是 ref：script setup 里 let 变量不会随赋值同步到模板上下文，
// 旧写法下「刷新二维码」按钮拿到的永远是初始的 null
const sock = ref(null)
let instanceId = null

const manifest = computed(() => manifests.value[dice.value] || {})
const metaOf = n => manifests.value[n] || {}
const pkgInfo = computed(() => pkgs.value[dice.value] || null)
const loginType = computed(() => manifest.value.login_type || 'none')
const isLoginRole = computed(() => role.value === 'login')
const curQQ = computed(() => {
  const i = instances.value.find(x => x.id === instanceId)
  return i?.qq || ''
})
// 登录端程序 = 在任意应用端 manifest 的 compatible_login 里出现过的程序（纯清单驱动，无名字分支）
const loginPrograms = computed(() => {
  const s = new Set()
  for (const m of Object.values(manifests.value))
    (m.compatible_login || []).forEach(o => { if (o !== 'builtin') s.add(o) })
  return Object.keys(manifests.value).filter(n => s.has(n))
})
const appPrograms = computed(() =>
  Object.keys(manifests.value).filter(n => !loginPrograms.value.includes(n)))
// 某程序兼容的协议登录端（排除 builtin：自带接入不算协议登录端）
const loginOptionsOf = n => (metaOf(n).compatible_login || []).filter(o => o !== 'builtin')
// 步骤一的程序下拉：按所选端类型给出对应清单
// 「创建并连接」时收窄到**与来源端兼容**的程序——否则用户可能建出一个连不上的实例
// （兼容矩阵是双向的：建登录端要看它是否在来源应用端的 compatible_login 里）
const rolePrograms = computed(() => {
  if (pairFrom.value) {
    return role.value === 'login'
      ? loginOptionsOf(pairFrom.value.dice)      // 来源应用端兼容的登录端
      : compatibleApps.value                      // 与来源登录端兼容的应用端
  }
  return isLoginRole.value ? loginPrograms.value : appPrograms.value
})
// 应用端可选的登录端兼容矩阵（同上口径，针对当前所选程序）
const loginOptions = computed(() => loginOptionsOf(dice.value))
// 接入通道：清单驱动（manifest.bot_modes）。官方通道由程序自身对接官方服务，
// 不需要登录端、也不需要面板写互联配置 —— 向导据此跳过「链接」环节。
const botModes = computed(() => manifest.value.bot_modes || [])
const botMode = ref('onebot')
const officialMode = computed(() =>
  botMode.value === 'official' && botModes.value.some(b => b.id === 'official'))
const officialGuide = computed(() =>
  (botModes.value.find(b => b.id === 'official') || {}).guide_url || '')
// 登录端候选 = 已创建且程序类型在兼容矩阵里的实例（login_ref 在后端即实例 id，用于画连线）
const loginCandidates = computed(() =>
  instances.value.filter(i => loginOptions.value.includes(i.dice)))
// 已被本实例关联的登录端不再出现在「添加」下拉里
const addableLogins = computed(() =>
  loginCandidates.value.filter(o => !pickedLinks.value.some(l => l.login_ref === o.id)))
// 登录端第三步：管理器内已存在、且与本登录端程序兼容的应用端实例
const appCandidates = computed(() => {
  if (!isLoginRole.value) return []
  return instances.value.filter(i => i.id !== instanceId
    && !loginPrograms.value.includes(i.dice)
    && (metaOf(i.dice).compatible_login || []).includes(dice.value))
})
const compatibleApps = computed(() => appPrograms.value.filter(n =>
  (metaOf(n).compatible_login || []).includes(dice.value)))
// 本登录端已登录的 QQ 账号（「创建并连接」之外的关联按账号分发）
const myAccounts = computed(() =>
  (instances.value.find(i => i.id === instanceId)?.accounts) || [])
// 默认选中项要在切换程序时同步，否则默认程序（未触发 change）永远拿不到该标记
const needAuthToken = computed(() => !!manifest.value.auth_token_conditional)
// ---------- 登录方式（扫码/ 账密）----------
// 能力来自后端 login_modes（由适配器方法给出，不是清单字段）。缺字段的老服务端
// 一律按只有扫码处理，不能因为前端先放开就会给用户一个写不进密码的假选项。
const loginModes = computed(() => manifest.value.login_modes || ['qrcode'])
const canPasswordLogin = computed(() => loginModes.value.includes('account'))
const canQrcodeLogin = computed(() => loginModes.value.includes('qrcode'))
// 只有账密可用的程序（如上游仅支持密码的协议端）不显示二选一，直接进账密表单
const showModePick = computed(() => canPasswordLogin.value && canQrcodeLogin.value)
const showQrcode = computed(() =>
  loginMode.value === 'qrcode' && canQrcodeLogin.value && loginType.value !== 'account')
// 账密可选协议：清单未声明时给一组通用候选（Lagrange 系用 Windows/Linux/MacOS）
const protocols = computed(() => manifest.value.login_protocols
  || [{ id: 'Windows', label: 'Windows' }, { id: 'Linux', label: 'Linux' },
      { id: 'macOS', label: 'macOS' }])
// 第三步是否需要登录环节：登录端几乎都要（webui 也要提示去哪登）；
// 应用端只有声明了 qrcode/account/webui 的才要，其余（external/none）直接进链接
const needLoginHere = computed(() => {
  if (officialMode.value) return false
  if (isLoginRole.value) return loginType.value !== 'none'
  return ['qrcode', 'account', 'webui'].includes(loginType.value)
})
const loginInstOf = lr => instances.value.find(i => i.id === lr) || null
const accountsOf = lr => loginInstOf(lr)?.accounts || []
const needsLoginEnd = computed(() => loginOptions.value.length > 0 && !pickedLinks.value.length)

// ---------- 步骤条 ----------
const stepNames = computed(() => {
  // 「创建并连接」跳回第一步时，步骤名点明是为谁建的，避免用户以为回到了全新流程
  if (pairFrom.value) {
    return role.value === 'login' ? ['选登录端', '下载部署', '登录并连接', '已启动']
      : [`为 ${pairFrom.value.dice} 选应用端`, '下载部署', '连接并启动', '已启动']
  }
  if (isLoginRole.value) return ['选登录端', '下载部署', '登录并连接', '已启动']
  return ['选应用端', '下载部署', '连接并启动', '已启动']
})
const curStepIdx = computed(() => Math.min(step.value, stepNames.value.length))
const resumeLabel = n => (n === 2 ? '下载部署' : n === 3 ? '登录并连接' : '连接并启动')

const canCreate = computed(() => !!dice.value && !!role.value)
const createLabel = computed(() => pkgInfo.value ? '解压本地包部署' : '下载部署')

const load = async () => {
  loading.value = true; loadError.value = ''
  try {
    const [m, inst, pend, pk] = await Promise.all(
      [listManifests(), listInstances(), listPending(), listPackages()])
    manifests.value = m || {}; instances.value = inst || []; pending.value = pend || []
    pkgs.value = Object.fromEntries((pk || []).filter(p => p.exists).map(p => [p.dice, p]))
    role.value = appPrograms.value.length ? 'app' : 'login'
    dice.value = rolePrograms.value[0] || ''
    step.value = dice.value ? 1 : 0
  } catch (e) {
    loadError.value = e.message || String(e)
  } finally {
    loading.value = false
  }
}
load()

// 程序包：上传后部署直接解压本地包，不再联网下载
const pkgUp = ref(null)             // 上传进度 {loaded, total, sent, startAt}
const pkgJob = ref(null)             // 当前上传任务（Promise.abort() 取消 XHR）
const fmtMB = b => (b / 1048576).toFixed(1)
const pkgPercent = computed(() => {
  const u = pkgUp.value
  if (!u) return 0
  if (u.sent) return 100                          // 已发完 → 满格（停在「校验中」）
  return u.total ? Math.min(99, Math.round(u.loaded / u.total * 100)) : 100
})
const pkgText = computed(() => {
  const u = pkgUp.value
  if (!u) return ''
  // 请求体发完后后端还要落盘并做整体校验，大包可达数十秒，必须说明，否则像卡死
  if (u.sent) return '已发送完毕，服务端正在落盘并校验（大包可能耗时较久，请勿关闭页面）…'
  const secs = (Date.now() - u.startAt) / 1000
  const rate = secs > 1 ? ` · ${fmtMB(u.loaded / secs)} MB/s` : ''
  const size = `已上传 ${fmtMB(u.loaded)}${u.total ? ` / ${fmtMB(u.total)} MB` : ' MB'}`
  return u.total ? `${size}（${pkgPercent.value}%${rate}）` : size + rate
})

const uploadPkg = e => {
  const file = e.target.files[0]
  e.target.value = ''                          // 允许重选同一文件再次触发 change
  if (!file) return
  pkgBusy.value = true; pkgMsg.value = ''
  pkgUp.value = { loaded: 0, total: file.size, sent: false, startAt: Date.now() }
  pkgJob.value = uploadPackage(dice.value, file, ({ loaded, total, sent }) => {
    const cur = pkgUp.value                    // sent 一旦为真不再回退；loaded 取单调最大值
    pkgUp.value = { loaded: Math.max(loaded, cur?.loaded || 0),
                    total: total || cur?.total || file.size,
                    sent: !!(sent || cur?.sent),
                    startAt: cur?.startAt || Date.now() }
  })
    .then(info => {
      pkgs.value = { ...pkgs.value, [dice.value]: info }
      pkgMsg.value = `已上传 ${info.size_mb} MB，部署时将直接解压该包。`
    })
    .catch(ex => { pkgMsg.value = ''; err.value = ex.message || String(ex) })
    .finally(() => { pkgBusy.value = false; pkgUp.value = null; pkgJob.value = null })
}

const cancelPkg = () => { pkgJob.value?.abort?.() }

const removePkgOf = name => {
  pkgBusy.value = true; pkgMsg.value = ''
  deletePackage(name)
    .then(() => {
      const next = { ...pkgs.value }; delete next[name]; pkgs.value = next
      pkgMsg.value = `已删除 ${name} 的本地包，下次部署将在线下载。`
    })
    .catch(ex => { err.value = ex.message || String(ex) })
    .finally(() => { pkgBusy.value = false })
}
const removePkg = () => removePkgOf(dice.value)

// 静默刷新实例/待续跑列表（启动完成后待续跑条目应消失）
const refreshLists = async () => {
  try {
    const [i, p] = await Promise.all([listInstances(), listPending()])
    instances.value = i || []; pending.value = p || []
  } catch { /* 静默：刷新失败不阻断页面 */ }
}

// 切换端类型：程序下拉要跟着换清单（旧程序名在新清单里不存在会一路错下去）
const pickRole = r => {
  if (role.value === r) return
  role.value = r
  botMode.value = 'onebot'
  dice.value = rolePrograms.value[0] || ''
  loginMode.value = 'qrcode'
}

// 第三步「创建并连接」：记下来源端并跳回第一步新建对端的一端。
// 不在第三步就地建——那要把部署/冲突/进度一整套 UI 复制一份，而复用第一步
// 天然就有这些；代价是要走一遍选程序（这里已按兼容性把候选收窄）。
const gotoCreate = target => {
  pairFrom.value = { id: instanceId, dice: dice.value, qq: curQQ.value, target }
  role.value = target
  botMode.value = 'onebot'
  dice.value = rolePrograms.value[0] || ''
  loginMode.value = 'qrcode'
  pickedApps.value = []; pickedLinks.value = []; addLoginRef.value = ''
  loggedIn.value = false; tokenSaved.value = false
  conflict.value = false; err.value = ''
  sock.value?.close(); sock.value = null
  stopDeployPoll(); deployMsg.value = ''; deployFail.value = ''
  step.value = 1
}

// 取消配对：回到普通的第一步（端类型卡片重新可选）
const cancelPair = () => {
  pairFrom.value = null
  role.value = appPrograms.value.length ? 'app' : 'login'
  dice.value = rolePrograms.value[0] || ''
  loginMode.value = 'qrcode'
}

// 切换登录方式：扫码↔账密。离开扫码页时关掉推送通道（否则后台继续等扫码消息）
const switchLoginMode = m => {
  if (loginMode.value === m) return
  loginMode.value = m
  qr.value = {}; verifyUrl.value = ''
  if (m === 'account') {
    sock.value?.close(); sock.value = null
    // 账密登录不需要二维码通道；LLBot 的 TOKEN 也与账密无关，一并清掉免得误触发保存
    tokenSaved.value = false
  } else if (loginType.value === 'qrcode' && instanceId && !loggedIn.value) {
    openLoginWS()
  }
}

const goOverview = () => (location.hash = '#/overview')

const reset = () => {
  sock.value?.close(); sock.value = null
  step.value = 1; err.value = ''; conflict.value = false
  qr.value = {}; verifyUrl.value = ''; preview.value = ''; manual.value = ''
  cred.value = { qq: '', password: '', protocol: '', auth_token: '' }
  conn.value = { direction: 'forward', addr: '', token: '' }
  loggedIn.value = false; tokenSaved.value = false; webuiInfo.value = null
  instanceId = null; pairFrom.value = null; loginMode.value = 'qrcode'
  role.value = appPrograms.value.length ? 'app' : 'login'
  dice.value = rolePrograms.value[0] || ''
  botMode.value = 'onebot'
  pickedApps.value = []; pickedLinks.value = []; addLoginRef.value = ''
  stopDeployPoll(); deployMsg.value = ''; deployFail.value = ''
  refreshLists()
}

// 断点续跑：跳到该实例所处环节。进入前先刷新列表，第三步取对端信息才不会是旧数据
const resume = async p => {
  sock.value?.close(); sock.value = null
  err.value = ''; conflict.value = false; preview.value = ''; manual.value = ''
  loggedIn.value = false; tokenSaved.value = false   // 续跑实例的 token 落盘状态未知，重新判定
  pickedApps.value = []; pickedLinks.value = []; addLoginRef.value = ''
  pairFrom.value = null; loginMode.value = 'qrcode'
  botMode.value = p.bot_mode || 'onebot'          // 续跑官方通道实例才能跳过链接
  stopDeployPoll(); deployMsg.value = ''; deployFail.value = ''
  await refreshLists()
  instanceId = p.id
  dice.value = p.dice
  role.value = loginPrograms.value.includes(p.dice) ? 'login' : 'app'
  const ns = Math.min(p.next_step || 1, 5)
  if (ns === 2) { step.value = 2; return }
  // next_step 3 与 5 都落在第三步。5 = 已过登录态（CONFIGURED/RUNNING）：
  // 此时不能再发 step3（会对已配置好的实例重跑 configure_login），只补齐链接区数据
  step.value = 3
  await enterStep3(ns >= 5).catch(e => { err.value = e.message || String(e) })
}

// 停止并删除未完成实例（如下载失败卡在部署中的）：未完成实例无存档，目录一并清理
const stopPending = p => guard(async () => {
  await delInstance(p.id, true, true, false)
  await refreshLists()
})

const guard = async fn => {                    // 统一转圈 + 报错，避免失败后界面无反馈卡死
  busy.value = true; err.value = ''
  try { await fn() } catch (e) { err.value = e.message || String(e) } finally { busy.value = false }
}

const create = () => guard(async () => {
  // 「创建并连接」：来源端是登录端时带上 login_ref，后端据此在实例上直接建好关联
  // （应用端随后经 login_ref 继承该登录端的端口与 token，两端才连得上）
  const body = { dice: dice.value, arch: manifest.value.arch || 'standalone',
                 bot_mode: officialMode.value ? 'official' : 'onebot' }
  if (pairFrom.value && pairFrom.value.target === 'app') body.login_ref = pairFrom.value.id
  const r = await createInstance(body)
  instanceId = r.id
  step.value = 2
  await doStep(2, {})
})

// ---------- 后端步骤调用（UI 三步 ↔ 后端 step2/3/4/5 的唯一通道）----------
// 不在此改 step.value：UI 步骤由调用点显式推进，避免两处状态机各写一遍
const doStep = async (n, payload) => {
  if (n === 2) { deployMsg.value = ''; deployFail.value = ''; startDeployPoll() }
  try {
    const r = await wizardStep(instanceId, n, payload)
    if (r.result === 'error') throw new Error(r.message || '操作失败')   // 如双击启动的竞态提示
    if (r.result === 'conflict') {
      conflict.value = true; conflictDir.value = r.dir || r.message || ''; return null
    }
    if (r.manual) manual.value = r.manual
    if (r.preview) preview.value = r.preview
    // 登录步后端判定无需登录（official / 登录已完成）时直接进入链接环节
    if (n === 3 && (r.skipped || r.needs_login === false)) onLoggedIn()
    return r
  } catch (e) {
    // 部署失败就地展示（重试/上传压缩包），不再抛给 guard 重复报错
    if (n === 2) { deployFail.value = e.message || String(e); return null }
    throw e
  } finally {
    if (n === 2) stopDeployPoll()
  }
}

// ---------- 部署失败出路：重试 / 上传压缩包后自动重试 ----------
const deployFail = ref('')
// 实例部署失败后仍处于 DEPLOYING 中间态，重发 step2 即可（本地包就位后同样走这条路径）
const retryDeploy = () => guard(async () => {
  err.value = ''
  await doStep(2, {})
})
// 失败面板里的上传：包落盘校验通过后立即重试部署，免去退回第一步重新创建
const uploadThenDeploy = e => {
  const file = e.target.files[0]
  e.target.value = ''                          // 允许重选同一文件再次触发 change
  if (!file) return
  pkgBusy.value = true; pkgMsg.value = ''
  pkgUp.value = { loaded: 0, total: file.size, sent: false, startAt: Date.now() }
  uploadPackage(dice.value, file, ({ loaded, total, sent }) => {
    const cur = pkgUp.value
    pkgUp.value = { loaded: Math.max(loaded, cur?.loaded || 0),
                    total: total || cur?.total || file.size,
                    sent: !!(sent || cur?.sent),
                    startAt: cur?.startAt || Date.now() }
  })
    .then(info => {
      pkgs.value = { ...pkgs.value, [dice.value]: info }
      pkgMsg.value = `已上传 ${info.size_mb} MB，正在用本地包重新部署…`
      retryDeploy()
    })
    .catch(ex => { pkgUp.value = null; deployFail.value = `上传失败：${ex.message || ex}` })
    .finally(() => { pkgBusy.value = false })
}

// 进入第三步的统一入口：
// · qrcode → 开 WS 推二维码（LLBot 的 TOKEN 先落盘再拉起）
// · 账密登录 → 不开WS，等用户填完提交（此时进程未起，登录随 step5 的启动完成）
// · 应用端无需登录 → 直接把登录环节标记完成，露出链接区
// alreadyConfigured=true 表示断点续跑时实例已过登录态：只取链接数据，不重发 step3
const enterStep3 = async (alreadyConfigured = false) => {
  await refreshLists()
  pickedLinks.value = (instances.value.find(i => i.id === instanceId)?.links || [])
    .map(l => ({ ...l }))
  // 账密登录不需要二维码通道，也不该预拉进程（进程由 step5 启动，凭据随之生效）
  if (!canPasswordLogin.value) loginMode.value = 'qrcode'
  if (alreadyConfigured) { loggedIn.value = true; return }
  if (needLoginHere.value) {
    if (showQrcode.value) {
      // TOKEN 已在第一步填写：进扫码页前先落盘（进程随后由 WS 自动拉起，天然带上 token）
      if (needAuthToken.value && cred.value.auth_token && !tokenSaved.value) await saveToken()
      openLoginWS()
    }
    return
  }
  // 无登录环节：仍推进一次 step3，让后端把状态机带到 CONFIGURED（幂等）
  await doStep(3, {})
}

// 登录完成（WS completed / 用户点「我已完成扫码」/ 后端判定 needs_login=false）
const onLoggedIn = () => {
  loggedIn.value = true
  refreshLists()
}

// ---------- 部署进度轮询：step2 同步部署期间 1s 拉一次，大包下载不再「假死」 ----------
const deployMsg = ref('')
// 下载进度：done/total 来自后端；total 为 0（无 Content-Length）时 dlPercent 为 null → 走不确定态
const dlDone = ref(0), dlTotal = ref(0), dlStartAt = ref(0)
// 速度/耗时每秒重算：进度条本身看不出「快还是卡住」，这两个数字才看得出
const dlSpeed = ref(''), dlElapsed = ref('')
const dlPercent = computed(() => {
  const t = dlTotal.value
  if (!t) return null
  return Math.min(100, Math.round((dlDone.value / t) * 100))
})
const fmtDur = s => (s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`)
const fmtSpeed = bps => (bps >= 1 << 20 ? `${(bps / (1 << 20)).toFixed(1)} MB/s`
                                 : `${Math.max(1, Math.round(bps / 1024))} KB/s`)

let deployTimer = null
let deployTarget = null
const stopDeployPoll = () => { if (deployTimer) { clearInterval(deployTimer); deployTimer = null } }
const resetDlProgress = () => {
  dlDone.value = 0; dlTotal.value = 0; dlSpeed.value = ''; dlElapsed.value = ''
  // 进入下载阶段才起算耗时：prepare/extract 阶段计时没有意义
  dlStartAt.value = Date.now()
}
const startDeployPoll = (target) => {
  stopDeployPoll()
  deployTarget = target || instanceId
  const id = deployTarget
  let lastDone = 0, lastAt = Date.now()
  resetDlProgress()
  deployTimer = setInterval(async () => {
    try {
      const p = await deployProgress(id)
      if (p.stage === 'download') {
        // 每次进入 download 阶段都重置计时起点：本地包命中直接跳到 extract 时不该显示下载耗时
        if (dlDone.value === 0) { dlStartAt.value = Date.now(); lastDone = 0; lastAt = Date.now() }
        dlDone.value = p.done || 0
        dlTotal.value = p.total || 0
        deployMsg.value = `正在下载程序包 ${fmtMB(p.done)}${p.total ? ' / ' + fmtMB(p.total) + ' MB' : ' MB'}…`
        const now = Date.now()
        const dt = (now - lastAt) / 1000
        // 速度用「本次轮询区间」算，比全程平均更贴近当下真实速率（刚起速时平均值会被拉偏）
        if (dt > 0 && p.done > lastDone) dlSpeed.value = `↓ ${fmtSpeed((p.done - lastDone) / dt)}`
        else if (p.done === lastDone) dlSpeed.value = '↓ 0 KB/s（可能已卡住，请耐心等待或稍后重试）'
        lastDone = p.done; lastAt = now
        dlElapsed.value = dlStartAt.value ? fmtDur((now - dlStartAt.value) / 1000) : ''
      } else if (p.stage === 'extract') {
        deployMsg.value = '下载完成，正在解压部署…'
        dlSpeed.value = ''; dlElapsed.value = ''
      } else if (p.stage === 'prepare') {
        deployMsg.value = '正在准备部署…'
        dlSpeed.value = ''; dlElapsed.value = ''
      } else if (p.stage === 'error') {
        // 后端下载异常时把原因带回来：否则用户只看到进度条停住，不知道该重试还是换网络
        deployFail.value = p.error || '下载失败，请重试或改为上传压缩包'
        stopDeployPoll()
      }
    } catch { /* 轮询失败不打扰主流程（真正失败由 doStep 的 catch 统一接管） */ }
  }, 1000)
}

// 登录端第三步：为勾选的已有应用端建立关联并重写其互联配置
// 必须在**本登录端自身配置写完之后**才逐个执行：应用端经 login_ref 继承的是登录端的
// conn_token（wizard._login_account_info），先关联会继承到空值并各自生成新 token，
// 两端不一致且连不上（这正是互联最难的排查点）。
// 任一实例失败即中止：半连上的状态比全不连更容易误判成"已配好"。
const applyAppLinks = async () => {
  for (const appId of linkTargets.value) {
    const inst = instances.value.find(i => i.id === appId)
    if (!inst) throw new Error(`应用端实例 ${appId} 不存在，请刷新页面重试`)
    const qq = pickAcct[appId] || null
    const links = (inst.links || []).map(l => ({ ...l }))
    const hit = links.find(l => l.login_ref === instanceId)
    if (hit) {
      // 已有指向本登录端的关联：就地改账号绑定，不追加第二条（重复项会在配置里留下重复端点）
      if ((hit.account_qq || '') === (qq || '')) continue
      hit.account_qq = qq
    } else {
      links.push({ login_ref: instanceId, account_qq: qq })
    }
    await linkInstance(appId, links)
    await wizardStep(appId, 4, {})                 // 重写该应用端的互联配置（继承本端端口/token）
  }
}

// 登录端第三步要反向关联的目标：勾选的已有应用端 + 「创建并连接」的来源应用端。
// 后者必须在这里补一次 link：来源应用端是旧实例，它并不自带指向本登录端的关联
// （建登录端时不能预设 login_ref——那时本实例还不存在）。
const linkTargets = computed(() => {
  const ids = [...pickedApps.value]
  if (pairFrom.value && pairFrom.value.target === 'login' && !ids.includes(pairFrom.value.id))
    ids.push(pairFrom.value.id)
  return ids
})

// ---------- 第三步收口：写互联配置 → 启动 → 已启动 ----------
const finish = () => guard(async () => {
  if (officialMode.value) {
    await doStep(5, {})
    step.value = 4
    afterStart()
    return
  }
  if (isLoginRole.value) {
    preview.value = ''
    // 先写本登录端自身配置（生成 conn_token），再反向关联：应用端继承的是它
    const r4 = await doStep(4, {})
    if (!r4) return
    await applyAppLinks()
  } else {
    const payload = { links: pickedLinks.value.map(l => ({
      login_ref: l.login_ref, account_qq: l.account_qq || null,
      direction: l.conn_direction || 'forward' })) }
    if (conn.value.token) payload.token = conn.value.token.trim()
    const r4 = await doStep(4, payload)
    if (!r4) return
  }
  await doStep(5, {})
  step.value = 4
  afterStart()
})

const afterStart = () => {
  // 就地反馈：WebUI 直达 + 令牌（actual_port 由启动日志回读，等一拍再取更准）
  setTimeout(() => instanceWebui(instanceId)
    .then(w => { webuiInfo.value = w })
    .catch(() => {}), 2500)
  refreshLists()
}

// ---------- 应用端第三步：关联列表增删 ----------
const addLink = () => {
  if (!addLoginRef.value) return
  pickedLinks.value = [...pickedLinks.value, { login_ref: addLoginRef.value,
                                              account_qq: '', conn_direction: 'forward' }]
  addLoginRef.value = ''
}
const removeLink = l => {
  pickedLinks.value = pickedLinks.value.filter(x => x !== l)
}

const resolve = useExisting => guard(async () => {   // 冲突二选一（主实例）
  deployMsg.value = ''; startDeployPoll()
  try {
    await wizardStep(instanceId, 2, { use_existing: useExisting })
  } finally {
    stopDeployPoll()
  }
  conflict.value = false
  step.value = 3
  await enterStep3()
})

const openLoginWS = () => {
  sock.value?.close()
  sock.value = connectWS(`/ws/login/${instanceId}`, m => {
    if (m.type === 'qrcode') qr.value = m.payload
    if (m.type === 'verify') verifyUrl.value = m.payload.url
    if (m.type === 'completed' || m.type === 'skipped') onLoggedIn()
    if (m.type === 'login_failed') {               // 账号登录失败：明确反馈而非卡在原页
      err.value = m.payload?.message || '登录失败，请检查账号信息后重试'
    }
  })
}

// ---------- 登录日志（可选，复用 /ws/logs 通道）----------
// 只在用户勾选时才连 WS：默认收起，且不该在用户没要求时占用服务端连接。
// 环形保留最近 LOG_MAX 行 —— 登录日志可能几分钟内刷屏几百行，无上限会撑爆内存。
const showLoginLog = ref(false)
const logFollow = ref(true)
const logLines = ref([])
const logBox = ref(null)
const logSock = ref(null)
const LOG_MAX = 500

const pushLogLine = (l) => {
  logLines.value.push(l)
  if (logLines.value.length > LOG_MAX)
    logLines.value = logLines.value.slice(-LOG_MAX)
  // 自动滚动要用 nextTick：此时 DOM 还没插入新行，直接设 scrollTop 无效
  if (logFollow.value) nextTick(() => {
    const el = logBox.value
    if (el) el.scrollTop = el.scrollHeight
  })
}
const clearLoginLog = () => { logLines.value = [] }

// 勾选/取消勾选时才开关连接：watch 而非 onMounted，避免默认就占用一条 WS
watch(showLoginLog, on => {
  logSock.value?.close()
  logSock.value = null
  if (!on) return
  if (!instanceId) return
  logLines.value = []
  logSock.value = connectWS(`/ws/logs/${instanceId}`, m => {
    if (m.type === 'line') pushLogLine({ text: m.text, error: m.error })
  })
})

const saveToken = async () => {                        // AUTH TOKEN 落盘（LLBot v8 出码前置条件）
  const r = await wizardStep(instanceId, 3, { qq: cred.value.qq, credentials: { ...cred.value } })
  if (r.result === 'error' || r.result === 'conflict')
    throw new Error(r.message || r.conflict || 'AUTH TOKEN 保存失败')
  tokenSaved.value = true
  if (r.manual) manual.value = r.manual
}

const doLogin = () => guard(async () => {              // 登录环节提交（含条件必填校验）
  // 账密登录：先本地校验再发，避免把明显不完整的表单打到后端
  if (loginMode.value === 'account') {
    if (!cred.value.qq.trim()) throw new Error('请填写 QQ 账号')
    if (!cred.value.password) throw new Error('请填写 QQ 密码')
    if (needAuthToken.value && !cred.value.auth_token)
      throw new Error('LLBot v8.0.9+ 必须填写 AUTH TOKEN')
    const payload = { qq: cred.value.qq.trim(),
                      login_mode: 'account',
                      credentials: { ...cred.value, qq: cred.value.qq.trim(),
                                     protocol: cred.value.protocol || protocols.value[0]?.id } }
    const r = await doStep(3, payload)
    // needs_login=false 表示后端判定无需再登录（配置已就位）；否则等 WS/用户确认
    if (r && (r.skipped || r.needs_login === false)) onLoggedIn()
    return
  }
  if (needAuthToken.value && !cred.value.auth_token) throw new Error('LLBot v8.0.9+ 必须填写 AUTH TOKEN')
  if (showQrcode.value && needAuthToken.value && !tokenSaved.value) {
    await saveToken()                        // 首次提交：保存 TOKEN 并重启进程出码，留在本环节扫码
    sock.value?.send('refresh')
    return
  }
  await doStep(3, { qq: cred.value.qq, credentials: { ...cred.value } })
})

onUnmounted(() => { sock.value?.close(); logSock.value?.close(); stopDeployPoll() })
</script>

<style scoped>
.wz-head { margin-bottom: 14px; }
.steps { display: flex; gap: 8px; flex-wrap: wrap; margin: 0; padding: 0; list-style: none; }
.steps li {
  padding: 4px 12px; border-radius: 999px; font-size: 12px;
  color: var(--muted); background: var(--code-bg); border: 1px solid var(--border);
}
.steps li.done { color: var(--ok); border-color: var(--step-done-border); background: var(--step-done-bg); }
.steps li.now { color: #fff; background: var(--brand); border-color: var(--brand); }
/* 官方机器人通道：接入清单比向导步骤更需要被看到 */
.official-tip { padding: 8px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--code-bg); margin-bottom: 10px; font-size: 13px; }
.official-tip ol { margin: 6px 0; padding-left: 20px; }
.official-tip li { margin: 3px 0; }
.wz-body { display: block; }
.field { max-width: 420px; margin-bottom: 14px; }
.field input, .field select { margin-bottom: 8px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 4px; }
.err {
  padding: 8px 12px; margin-bottom: 14px; color: var(--danger);
  background: var(--err-bg); border: 1px solid var(--err-border); border-radius: 8px;
}
.warn { color: var(--warn); }
.qr img { max-width: 260px; display: block; margin-bottom: 10px; background: var(--panel); }
.dialog { padding: 14px; margin-bottom: 14px; border: 1px solid var(--danger); border-radius: 8px; }
.deploy-fail { border-color: var(--warn); }
.fail-msg { color: var(--danger); font-weight: 600; margin-top: 0; }
.upload-btn {
  display: inline-flex; align-items: center; padding: 6px 14px; cursor: pointer;
  border: 1px solid var(--brand); border-radius: 8px; color: var(--brand); font-size: 14px;
}
.upload-btn input { display: none; }
.upload-btn.disabled { opacity: .5; pointer-events: none; }
.pending { margin-bottom: 14px; padding: 10px 14px; border: 1px solid var(--warn); border-radius: 8px; }
.pkg { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.pkg-ok { color: var(--ok); }
/* 上游侧已知问题：要比 hint 更显眼，又不能像报错（用户还没做错什么） */
.known-issue {
  padding: 7px 10px; border-radius: 8px;
  background: var(--code-bg); border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  color: var(--text); font-size: 13px;
}
.known-issue a { color: var(--brand); }
/* 下载前探测：把「将要下什么」摆出来，别让用户点了才知道下错 */
.pkgprobe {
  padding: 8px 10px; margin-bottom: 6px;
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px;
}
.pkgprobe p { margin: 0 0 4px; font-size: 13px; }
.pkgprobe p:last-child { margin-bottom: 0; }
.pkgprobe code { word-break: break-all; }   /* 资产名可能很长，别撑破布局 */
.pkgprobe .ops { margin-top: 6px; }
.adv-box { max-width: 480px; margin-bottom: 14px; }
.adv-box summary { cursor: pointer; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.adv-box[open] summary { margin-bottom: 10px; }
.start-info { margin-bottom: 12px; }
.webui-link { color: var(--brand); }
.tok { user-select: all; font-family: ui-monospace, Menlo, Consolas, monospace; }
.pending-item { display: flex; gap: 10px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.dialog code { font-family: ui-monospace, Menlo, Consolas, monospace; }
pre.preview {
  padding: 12px; margin: 0 0 14px; background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 8px; font-family: ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap;
}

/* ---------- 步骤一：端类型二选一 ---------- */
.pair-banner {
  display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
  padding: 10px 14px; margin-bottom: 14px;
  background: var(--brand-weak); border: 1px solid var(--brand); border-radius: var(--radius);
}
.pair-banner b { color: var(--brand); }
.pair-banner .hint { flex: 1 1 auto; }
.role-pick { display: flex; gap: 10px; flex-wrap: wrap; max-width: 560px; }
.role-card {
  flex: 1 1 220px; display: flex; flex-direction: column; gap: 4px;
  padding: 12px 14px; text-align: left; cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel-2);
}
.role-card b { font-size: 15px; }
.role-card span { font-size: 12px; color: var(--muted); font-weight: 400; }
.role-card:hover { border-color: var(--brand); }
.role-card.on { border-color: var(--brand); background: var(--brand-weak); color: var(--brand); }
.role-card.on span { color: var(--brand); opacity: .85; }

/* ---------- 步骤三：登录 / 链接 / 已启动 ---------- */
.login-box, .link-box {
  padding: 14px 16px; margin-bottom: 16px;
  background: var(--panel-2); border: 1px solid var(--border); border-radius: var(--radius);
}
.login-box h4, .link-box h4 { margin: 0 0 10px; font-size: 14px; }
/* 登录方式二选一（扫码 / 账密） */
.mode-pick { display: flex; gap: 8px; flex-wrap: wrap; }
.mode-btn {
  padding: 6px 16px; cursor: pointer;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--panel); color: var(--muted); font-weight: 500;
}
.mode-btn:hover { border-color: var(--brand); color: var(--brand); }
.mode-btn.on { background: var(--brand); border-color: var(--brand); color: #fff; }
.risk-note {
  padding: 8px 10px; margin-bottom: 8px;
  background: var(--code-bg); border: 1px solid var(--border);
  border-left: 3px solid var(--warn); border-radius: 8px;
}
.link-box .field { max-width: 520px; }
.ok-line { margin: 0 0 14px; color: var(--ok); font-weight: 600; }
.hint.none {
  padding: 10px 12px; background: var(--code-bg);
  border: 1px dashed var(--border); border-radius: 8px;
}
.pick-row {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  padding: 7px 0; border-bottom: 1px solid var(--border);
}
.pick { display: flex; gap: 8px; align-items: center; margin: 0; flex: 1 1 auto; cursor: pointer; }
.pick input { width: auto; }
.pick span { color: var(--text); font-size: 13px; }
.pick-row select.acct { width: auto; min-width: 170px; }
.conn-peer {
  padding: 8px 12px; margin-bottom: 12px; font-size: 13px;
  border: 1px solid var(--step-done-border, var(--border));
  background: var(--step-done-bg, var(--code-bg)); border-radius: 8px;
}
.peer-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.peer-head button.lnk { margin-left: auto; }
.peer-grid { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 8px; }
.peer-grid > div { flex: 1 1 200px; max-width: 260px; }
.done-card {
  display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
  padding: 12px 14px; margin-bottom: 14px;
  background: var(--step-done-bg); border: 1px solid var(--step-done-border); border-radius: 8px;
}
.done-card b { color: var(--ok); font-size: 15px; }
.done-card code { font-family: ui-monospace, Menlo, Consolas, monospace; }

/* ---------- 登录日志（可选，默认收起）---------- */
.login-logs { margin-top: 12px; padding-top: 10px; border-top: 1px dashed var(--border); }
.ll-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.ll-head .ops { margin: 0; }          /* 复用 .ops 但不引入它自带的 margin-top */
.logbox {
  margin-top: 8px; padding: 8px 10px;
  height: 220px; overflow-y: auto;      /* 固定高度：不能把登录表单顶出视口 */
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px;
  font-size: 12.5px; line-height: 1.5;
}
.log-line { margin: 0 0 2px; white-space: pre-wrap; word-break: break-all; }
.log-err { color: var(--danger); }
</style>