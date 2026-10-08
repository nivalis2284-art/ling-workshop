import {render,screen,waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi,test,expect} from "vitest";
import {LingApi} from "../api/client";
import {IdentityEditor} from "../features/identity/IdentityEditor";

test("owner self-state correction records evidence before a protected candidate and explicit approval",async()=>{
 const requests:{path:string;body:any}[]=[];
 vi.stubGlobal("fetch",vi.fn(async(input,init)=>{const path=String(input);const body=JSON.parse(init?.body||"{}");requests.push({path,body});
 return new Response(JSON.stringify(path.endsWith("/sources")?{id:"source",revision:1}:path.endsWith("/proposals")?{id:"candidate",status:"awaiting_owner"}:{id:"candidate",status:"applied_owner"}),{status:200,headers:{"Content-Type":"application/json"}});
 }));
 const refresh=vi.fn(async()=>{});render(<IdentityEditor api={new LingApi("fixture-owner",()=>{})} agentId="a" revision={2} refresh={refresh}/>);
 await userEvent.selectOptions(screen.getByLabelText("调整哪项认识"),"identity.description");
 await userEvent.type(screen.getByLabelText("当前希望采用的内容"),"更喜欢先核实依据。");
 await userEvent.click(screen.getByRole("button",{name:"审阅这项设置"}));
 expect(requests).toHaveLength(0);
 await userEvent.click(screen.getByRole("button",{name:"确认采用"}));
 await waitFor(()=>expect(refresh).toHaveBeenCalled());
 expect(requests.map(r=>r.path)).toEqual(["/api/sources","/api/proposals","/api/proposals/candidate/decision"]);
 expect(requests[1].body.sources).toEqual([{id:"source",revision:1}]);expect(requests[2].body.expected_revision).toBe(2);
 vi.unstubAllGlobals();
});
