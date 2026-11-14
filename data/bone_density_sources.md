# HALO：辐射阈值和骨密度数据来源（2026-11-14 整理）

## 辐射

NASA 原值（NASA-STD-3001 Vol.1 Rev C，2023-09-15）
- 职业生涯有效剂量 < 600 mSv，[V1 4030]，第 40 页
- 单次太阳粒子事件 (SPE) 有效剂量 < 250 mSv，[V1 4031]，第 43 页
- ALARA 原则，[V1 4029]，第 40 页
- 表 4.8-1（第 42 页，30 天 / 1 年 / 职业生涯，mGy-Eq）：造血器官 250 / 500 / 不适用；循环系统 250 / 500 / 1000；晶状体 1000 / 2000 / 4000；皮肤 1500 / 3000 / 6000；中枢神经 500 / 1000 / 1500 mGy
- 原文：https://standards.nasa.gov/sites/default/files/standards/NASA/C//nasa-std-3001-vol-1-rev-c-signature.pdf
- 辅助：OCHMO-TB-020 Rev E 辐射防护技术简报（2022-12-27）https://www.nasa.gov/wp-content/uploads/2023/03/radiation-protection-technical-brief-ochmo.pdf

ISS 基线（DLR/ESA 测量，不是 NASA 文件）
- Berger et al. 2017, J. Space Weather Space Clim. 7, A8：Columbus 舱 DOSTEL 吸收剂量最高约 286 µGy/天，剂量当量 647 µSv/天（2016 年 5 月）；平均品质因数 GCR 3.13、SAA 1.20
- https://www.swsc-journal.org/articles/swsc/full_html/2017/01/swsc160046/swsc160046.html
- Kavya 的 radiation_5min.csv 来源就是 DOSTEL-1（NASA_RadLab_DosTel1_ISS_Columbus），字段 dose_rate_ugy_h，单位一致

推出来的部分（我们自己定的，演示时要说明）
- µGy → µSv 换算系数 2.3（= 647/286，同一篇论文），近似值
- 告警三档见 radiation_thresholds.json 的 halo_alert_rules，每条都写了依据

## 骨密度

NASA 标准（NASA-STD-3001 Vol.1 Rev C，4.7 节，第 39–40 页）
- [V1 4027] 飞行中髋部和腰椎 BMD 保持在飞行前的 95% 以上，股骨颈 90% 以上
- [V1 4026] 飞行前 T 值要与同年龄、性别、族群人群一致（没给具体数字）
- [V1 4028] 返回后康复以恢复到飞行前基线为目标

骨质流失速率（模拟参数）
- NASA HRP 证据报告《Risk of Bone Fracture due to Spaceflight-induced Changes to Bone》（2017）：负重部位平均每月 1–1.5%（第 4 页）；表 4（第 22 页）有 ARED 锻炼时腰椎 −0.32%/月（SD 0.44）、转子 −0.50%/月（SD 0.40），n=11；ARED 之前腰椎 −1.06%/月、转子 −1.56%/月
- https://ntrs.nasa.gov/api/citations/20170004597/downloads/20170004597.pdf
- 交叉验证：Stavnichuk et al. 2020 npj Microgravity 6:13 荟萃分析（约 148 名宇航员），下肢 −0.8%/月；原始数据"合理请求可提供"，没公开下载 https://pmc.ncbi.nlm.nih.gov/articles/PMC7200725

能不能直接下载宇航员骨密度原始数据
- 没找到。data.nasa.gov 上标着骨密度的数据集是大鼠（STS-62，LSDA mission 104）；OSDR 的 OSD-417 是小鼠（RR-5）。都不是人。
- NASA 的人体（宇航员）数据在 JSC 的 LSDA，走数据申请；申请页我打不开，没看到细则。周日截止前基本拿不到。

所以 simulated_bone_density.csv 是按上面速率生成的模拟数据（data_type = SIMULATED）
- SIM-A：按 ARED 时代速率，6 个月后全程正常
- SIM-B：按 ARED 之前速率（模拟锻炼中断），第 90 天出现"注意"，第 180 天髋部和腰椎跌破 95% 变成"处理"
- 髋部总和股骨颈没有单独的每月速率，暂用转子速率代替，这一点是我们的近似
- ISS 上骨密度只在飞行前后测（DXA/QCT），飞行中没有；HALO 的飞行中骨密度是估算值，演示时要标明
