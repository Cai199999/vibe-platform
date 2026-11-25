import 'leaflet/dist/leaflet.css'
import { useState } from "react"
import "./visual.css"
import { MapContainer, TileLayer, Marker, Popup } from 'react-leaflet'
import DataList from './DataList.jsx'

const VISUAL_ACTIONS = [{"id": "int_935b4fbad633", "action": "submit", "endpoint": "/api/v1/users/login", "method": "POST", "targetPageId": ""}, {"id": "int_363ea9cc4212", "action": "submit", "endpoint": "/api/v1/facilities", "method": "POST", "targetPageId": ""}, {"id": "int_595efceddd5b", "action": "call_api", "endpoint": "/api/v1/facilities", "method": "GET", "targetPageId": ""}, {"id": "int_b4554b7e83be", "action": "call_api", "endpoint": "/api/v1/facilities/{facility_id}", "method": "GET", "targetPageId": ""}, {"id": "int_ba00b59a39cc", "action": "call_api", "endpoint": "/api/v1/facilities/{facility_id}/facility-images", "method": "GET", "targetPageId": ""}];

async function runVisualAction(actionId) {
  const action = VISUAL_ACTIONS.find((item) => item.id === actionId);
  if (!action) return;
  if (action.action === 'navigate' && action.endpoint) {
    window.location.assign(action.endpoint);
    return;
  }
  if (action.endpoint) {
    try {
      const response = await fetch(action.endpoint, {
        method: action.method || 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: action.method === 'GET' ? undefined : JSON.stringify({}),
      });
      if (!response.ok) {
        console.error('[visual-action] failed', action.id, response.status);
      }
    } catch (error) {
      console.error('[visual-action] request failed', action.id, error);
    }
    return;
  }
  console.info('[visual-action]', action.id, action.action);
}

function PageGIS({ onNavigate }) {
  return (
    <>
      <main className={"visual-node visual-page min-h-screen w-full bg-[var(--color-surface)] text-[var(--color-text)] bg-white block p-[32px] max-w-[1180px] mx-auto"} data-oid="oid_nd_b307f616a3f8882291cb" data-node-id="nd_b307f616a3f8882291cb" data-source="src/App.jsx:36" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="page">
        <section className={"visual-node visual-section rounded-xl border bg-white flex flex-row items-stretch justify-start flex-nowrap gap-[16px] p-[20px]"} data-oid="oid_nd_064159f8de75a306074b" data-node-id="nd_064159f8de75a306074b" data-source="src/App.jsx:37" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
          <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_1fd2c06605266a829ab8" data-node-id="nd_1fd2c06605266a829ab8" data-source="src/App.jsx:38" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
            {"GIS 任务管理平台"}
          </p>
          <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_a42f6d81e8e5e818e94f" data-node-id="nd_a42f6d81e8e5e818e94f" data-source="src/App.jsx:41" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
            {"由 Architect 生成的虚拟骨架"}
          </p>
        </section>
        <div className={"visual-node visual-grid grid grid-cols-2 gap-[24px] p-[24px_0px]"} data-oid="oid_nd_5ba9b1499d3abcefb7ba" data-node-id="nd_5ba9b1499d3abcefb7ba" data-source="src/App.jsx:45" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="grid">
          <section className={"visual-node visual-section rounded-xl border bg-slate-50 flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_6b16a7c37fde9ec4c192" data-node-id="nd_6b16a7c37fde9ec4c192" data-source="src/App.jsx:46" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
            <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_40df2cd65158e29e7cd0" data-node-id="nd_40df2cd65158e29e7cd0" data-source="src/App.jsx:47" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
              {"导航"}
            </p>
            <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_66e619fec35ad9ca3d25" data-node-id="nd_66e619fec35ad9ca3d25" data-source="src/App.jsx:50" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
              {"模块入口待补充"}
            </p>
          </section>
          <section className={"visual-node visual-section flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_72f1aa5ab41940bff27f" data-node-id="nd_72f1aa5ab41940bff27f" data-source="src/App.jsx:54" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
            <form className={"visual-node visual-form rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_7c4a74529603e28ade70" data-node-id="nd_7c4a74529603e28ade70" data-source="src/App.jsx:55" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="form" onClick={() => runVisualAction("int_935b4fbad633")} onSubmit={(event) => event.preventDefault()}>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_5c1e9cf5d01e4f89e778" data-node-id="nd_5c1e9cf5d01e4f89e778" data-source="src/App.jsx:56" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"登录表单"}
              </p>
              <input className={"visual-node visual-input w-full rounded-[var(--radius-sm)] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-[var(--typography-body)] text-[var(--color-text)] rounded-lg border-slate-300 block"} data-oid="oid_nd_82b7f5001a220d9608cb" data-node-id="nd_82b7f5001a220d9608cb" data-source="src/App.jsx:59" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="input" aria-label={"请输入登录表单"} type="text" placeholder={"请输入登录表单"} />
              <button className={"visual-node visual-button inline-flex items-center justify-center border border-transparent px-4 py-2 text-[var(--typography-body)] font-semibold cursor-pointer rounded-lg bg-blue-600 text-white block"} data-oid="oid_nd_798c10e881c408ba24d8" data-node-id="nd_798c10e881c408ba24d8" data-source="src/App.jsx:60" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="button">
                {"提交"}
              </button>
            </form>
            <div className={"visual-node visual-component rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[8px] p-[16px]"} data-oid="oid_nd_cdd8e73b377027fe80cf" data-node-id="nd_cdd8e73b377027fe80cf" data-source="src/App.jsx:64" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="component">
              <MapContainer center={[39.9042, 116.4074]} zoom={12} style={{ height: 360, width: '100%' }}>
                <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
              </MapContainer>
              <div hidden>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_7cd6dca6228544d24e4e" data-node-id="nd_7cd6dca6228544d24e4e" data-source="src/App.jsx:69" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"地图画布"}
              </p>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_9e369e6a9d042d455d52" data-node-id="nd_9e369e6a9d042d455d52" data-source="src/App.jsx:72" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"Leaflet + OSM 底图，渲染设施点与巡检路线"}
              </p>
              </div>
            </div>
            <div className={"visual-node visual-component rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[8px] p-[16px]"} data-oid="oid_nd_143f3561c6bea3d97446" data-node-id="nd_143f3561c6bea3d97446" data-source="src/App.jsx:77" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="component">
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_09abbc15a484e56ae95b" data-node-id="nd_09abbc15a484e56ae95b" data-source="src/App.jsx:78" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"图层切换器"}
              </p>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_967880df3dd1db4304bc" data-node-id="nd_967880df3dd1db4304bc" data-source="src/App.jsx:81" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"底图/设施点/巡检路线开关"}
              </p>
            </div>
            <div className={"visual-node visual-component rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[8px] p-[16px]"} data-oid="oid_nd_531062ef8369f8d7ab9f" data-node-id="nd_531062ef8369f8d7ab9f" data-source="src/App.jsx:85" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="component" onClick={() => runVisualAction("int_595efceddd5b")}>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_1f984c650a0308902267" data-node-id="nd_1f984c650a0308902267" data-source="src/App.jsx:86" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"设施筛选面板"}
              </p>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_597adf8cea4ce92ef250" data-node-id="nd_597adf8cea4ce92ef250" data-source="src/App.jsx:89" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"类型、状态、框选/当前位置查询"}
              </p>
            </div>
            <form className={"visual-node visual-form rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_7fca7f284681ddaa0f0f" data-node-id="nd_7fca7f284681ddaa0f0f" data-source="src/App.jsx:93" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="form" onClick={() => runVisualAction("int_363ea9cc4212")} onSubmit={(event) => event.preventDefault()}>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_001984a7173e76e3a3d7" data-node-id="nd_001984a7173e76e3a3d7" data-source="src/App.jsx:94" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"设施表单"}
              </p>
              <input className={"visual-node visual-input w-full rounded-[var(--radius-sm)] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-[var(--typography-body)] text-[var(--color-text)] rounded-lg border-slate-300 block"} data-oid="oid_nd_8214d1cacd8681dfe3c7" data-node-id="nd_8214d1cacd8681dfe3c7" data-source="src/App.jsx:97" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="input" aria-label={"请输入设施表单"} type="text" placeholder={"请输入设施表单"} />
              <button className={"visual-node visual-button inline-flex items-center justify-center border border-transparent px-4 py-2 text-[var(--typography-body)] font-semibold cursor-pointer rounded-lg bg-blue-600 text-white block"} data-oid="oid_nd_db063590ac68813e1101" data-node-id="nd_db063590ac68813e1101" data-source="src/App.jsx:98" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="button">
                {"提交"}
              </button>
            </form>
            <ul className={"visual-node visual-list m-0 list-none p-0 rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[8px] p-[16px]"} data-oid="oid_nd_faa7566c51b7e5fed256" data-node-id="nd_faa7566c51b7e5fed256" data-source="src/App.jsx:102" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="list" onClick={() => runVisualAction("int_b4554b7e83be")}>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_26e1574ebb9f99689908" data-node-id="nd_26e1574ebb9f99689908" data-source="src/App.jsx:103" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"列表内容"}
              </p>
            </ul>
            <div className={"visual-node visual-component rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[8px] p-[16px]"} data-oid="oid_nd_d76781d30015230f34a5" data-node-id="nd_d76781d30015230f34a5" data-source="src/App.jsx:107" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="component" onClick={() => runVisualAction("int_ba00b59a39cc")}>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_0b8a2cac497801646562" data-node-id="nd_0b8a2cac497801646562" data-source="src/App.jsx:108" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"影像上传"}
              </p>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_ba767a973dad9c7de7d9" data-node-id="nd_ba767a973dad9c7de7d9" data-source="src/App.jsx:111" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"上传并展示设施关联影像"}
              </p>
            </div>
            <div className={"visual-node visual-component rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[8px] p-[16px]"} data-oid="oid_nd_27e75bb78c3305463c15" data-node-id="nd_27e75bb78c3305463c15" data-source="src/App.jsx:115" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="component">
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_f694e67ab93aaa4a2e54" data-node-id="nd_f694e67ab93aaa4a2e54" data-source="src/App.jsx:116" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"工单列表"}
              </p>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_8548b3e4ecd5445d4658" data-node-id="nd_8548b3e4ecd5445d4658" data-source="src/App.jsx:119" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"状态、指派人、截止时间、逾期标记"}
              </p>
            </div>
            <form className={"visual-node visual-form rounded-xl border bg-white flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_0cbafe8d769e8e19896f" data-node-id="nd_0cbafe8d769e8e19896f" data-source="src/App.jsx:123" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="form" onSubmit={(event) => event.preventDefault()}>
              <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_4ba17f4840b92ae92a52" data-node-id="nd_4ba17f4840b92ae92a52" data-source="src/App.jsx:124" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
                {"工单创建表单"}
              </p>
              <input className={"visual-node visual-input w-full rounded-[var(--radius-sm)] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-[var(--typography-body)] text-[var(--color-text)] rounded-lg border-slate-300 block"} data-oid="oid_nd_c5550854a22d273b22c7" data-node-id="nd_c5550854a22d273b22c7" data-source="src/App.jsx:127" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="input" aria-label={"请输入工单创建表单"} type="text" placeholder={"请输入工单创建表单"} />
              <button className={"visual-node visual-button inline-flex items-center justify-center border border-transparent px-4 py-2 text-[var(--typography-body)] font-semibold cursor-pointer rounded-lg bg-blue-600 text-white block"} data-oid="oid_nd_272a582f0e33b77ecc37" data-node-id="nd_272a582f0e33b77ecc37" data-source="src/App.jsx:128" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="button">
                {"提交"}
              </button>
            </form>
            <button className={"visual-node visual-button inline-flex items-center justify-center border border-transparent px-4 py-2 text-[var(--typography-body)] font-semibold cursor-pointer rounded-lg bg-blue-600 text-white block"} data-oid="oid_nd_1479d20134f647a1504b" data-node-id="nd_1479d20134f647a1504b" data-source="src/App.jsx:132" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="button">
              {"工单状态按钮"}
            </button>
          </section>
        </div>
      </main>
      <DataList endpoint="/api/v1/facilities" />
    </>
  )
}

function PagePage2({ onNavigate }) {
  return (
    <main className={"visual-node visual-page min-h-screen w-full bg-[var(--color-surface)] text-[var(--color-text)] bg-white block p-[32px] max-w-[1180px] mx-auto"} data-oid="oid_nd_d0d6020b7a6d7217e498" data-node-id="nd_d0d6020b7a6d7217e498" data-source="src/App.jsx:145" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="page">
      <section className={"visual-node visual-section rounded-xl border bg-white flex flex-row items-stretch justify-start flex-nowrap gap-[16px] p-[20px]"} data-oid="oid_nd_749bcca3c6be1cb20b89" data-node-id="nd_749bcca3c6be1cb20b89" data-source="src/App.jsx:146" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_a8352415831f8322a52d" data-node-id="nd_a8352415831f8322a52d" data-source="src/App.jsx:147" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"GIS 任务管理平台"}
        </p>
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_c56c09b63e7e27d3de38" data-node-id="nd_c56c09b63e7e27d3de38" data-source="src/App.jsx:150" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"用户名口令登录，按角色跳转"}
        </p>
      </section>
      <section className={"visual-node visual-section flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_950134ede14a0c351f72" data-node-id="nd_950134ede14a0c351f72" data-source="src/App.jsx:154" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_c444d1db70f387542a05" data-node-id="nd_c444d1db70f387542a05" data-source="src/App.jsx:155" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"登录页"}
        </p>
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_e66837c5fbbd7d24fd48" data-node-id="nd_e66837c5fbbd7d24fd48" data-source="src/App.jsx:158" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"该路由已创建，业务内容等待增量审查补充"}
        </p>
      </section>
    </main>
  )
}

function PagePage3({ onNavigate }) {
  return (
    <main className={"visual-node visual-page min-h-screen w-full bg-[var(--color-surface)] text-[var(--color-text)] bg-white block p-[32px] max-w-[1180px] mx-auto"} data-oid="oid_nd_785d396005e2de2ddfc9" data-node-id="nd_785d396005e2de2ddfc9" data-source="src/App.jsx:168" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="page">
      <section className={"visual-node visual-section rounded-xl border bg-white flex flex-row items-stretch justify-start flex-nowrap gap-[16px] p-[20px]"} data-oid="oid_nd_cb32c80a48fdab049565" data-node-id="nd_cb32c80a48fdab049565" data-source="src/App.jsx:169" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_ce27876bb370191af964" data-node-id="nd_ce27876bb370191af964" data-source="src/App.jsx:170" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"GIS 任务管理平台"}
        </p>
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_784c86b97334ce076cd4" data-node-id="nd_784c86b97334ce076cd4" data-source="src/App.jsx:173" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"设施列表、筛选、CRUD 与影像"}
        </p>
      </section>
      <section className={"visual-node visual-section flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_b489b6e8ad5d3e77e280" data-node-id="nd_b489b6e8ad5d3e77e280" data-source="src/App.jsx:177" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_72e2a4a9cb6bf20b4437" data-node-id="nd_72e2a4a9cb6bf20b4437" data-source="src/App.jsx:178" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"设施管理"}
        </p>
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_2d865a5287fec6fd1b23" data-node-id="nd_2d865a5287fec6fd1b23" data-source="src/App.jsx:181" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"该路由已创建，业务内容等待增量审查补充"}
        </p>
      </section>
    </main>
  )
}

function PagePage4({ onNavigate }) {
  return (
    <main className={"visual-node visual-page min-h-screen w-full bg-[var(--color-surface)] text-[var(--color-text)] bg-white block p-[32px] max-w-[1180px] mx-auto"} data-oid="oid_nd_0a521a2538ac3a0b9e6e" data-node-id="nd_0a521a2538ac3a0b9e6e" data-source="src/App.jsx:191" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="page">
      <section className={"visual-node visual-section rounded-xl border bg-white flex flex-row items-stretch justify-start flex-nowrap gap-[16px] p-[20px]"} data-oid="oid_nd_d339e58bad3a1fde42a1" data-node-id="nd_d339e58bad3a1fde42a1" data-source="src/App.jsx:192" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_3499506bb9273fcad55d" data-node-id="nd_3499506bb9273fcad55d" data-source="src/App.jsx:193" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"GIS 任务管理平台"}
        </p>
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_3af6bada430a1fccf9ef" data-node-id="nd_3af6bada430a1fccf9ef" data-source="src/App.jsx:196" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"工单列表、创建指派与状态流转"}
        </p>
      </section>
      <section className={"visual-node visual-section flex flex-col items-stretch justify-start flex-nowrap gap-[12px] p-[20px]"} data-oid="oid_nd_88ffab4eef4c0f9d16b8" data-node-id="nd_88ffab4eef4c0f9d16b8" data-source="src/App.jsx:200" data-source-file="src/App.jsx" data-source-kind="container" data-visual-type="section">
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_a554e53babeb4787b71d" data-node-id="nd_a554e53babeb4787b71d" data-source="src/App.jsx:201" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"巡检工单"}
        </p>
        <p className={"visual-node visual-text m-0 text-[var(--typography-body)] leading-relaxed text-[var(--color-text)] text-sm text-slate-700 block"} data-oid="oid_nd_54fa6e2938e3154bc3d8" data-node-id="nd_54fa6e2938e3154bc3d8" data-source="src/App.jsx:204" data-source-file="src/App.jsx" data-source-kind="element" data-visual-type="text">
          {"该路由已创建，业务内容等待增量审查补充"}
        </p>
      </section>
    </main>
  )
}

const VISUAL_PAGES = [
  { id: "pg_881b4bacef1b4f286c85", path: "/", title: "GIS 任务管理平台", Component: PageGIS },
  { id: "pg_cc8ab431d7132ee2bae6", path: "/login", title: "登录页", Component: PagePage2 },
  { id: "pg_42dbf160213620dedbce", path: "/facilities", title: "设施管理", Component: PagePage3 },
  { id: "pg_11983be87be5bc6dd482", path: "/tasks", title: "巡检工单", Component: PagePage4 },
];

export default function App() {
  const [activePageId, setActivePageId] = useState(
    VISUAL_PAGES[0]?.id || '',
  );
  const activePage =
    VISUAL_PAGES.find((page) => page.id === activePageId) ||
    VISUAL_PAGES[0];
  if (!activePage) return null;
  const ActivePage = activePage.Component;
  return (
    <>
      <nav className="visual-page-nav">
        {VISUAL_PAGES.map((page) => (
          <button
            key={page.id}
            type="button"
            className={page.id === activePageId ? 'is-active' : ''}
            onClick={() => setActivePageId(page.id)}
          >
            {page.title}
          </button>
        ))}
      </nav>
      <ActivePage onNavigate={setActivePageId} />
    </>
  );
}
