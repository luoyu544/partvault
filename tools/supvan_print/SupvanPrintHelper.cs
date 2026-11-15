using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading;
using Supvan.T50PRO.SDK;

namespace SupvanPrintHelper
{
    [DataContract]
    public class TextJob
    {
        [DataMember(Name = "x")] public decimal X { get; set; }
        [DataMember(Name = "y")] public decimal Y { get; set; }
        [DataMember(Name = "w")] public decimal Width { get; set; }
        [DataMember(Name = "h")] public decimal Height { get; set; }
        [DataMember(Name = "text")] public string Text { get; set; }
        [DataMember(Name = "size")] public string Size { get; set; }
        [DataMember(Name = "bold")] public bool Bold { get; set; }
        [DataMember(Name = "align")] public int Align { get; set; }
        [DataMember(Name = "font")] public string Font { get; set; }
        [DataMember(Name = "wrap")] public bool Wrap { get; set; }
    }

    [DataContract]
    public class QrJob
    {
        [DataMember(Name = "data")] public string Data { get; set; }
        [DataMember(Name = "x")] public decimal X { get; set; }
        [DataMember(Name = "y")] public decimal Y { get; set; }
        [DataMember(Name = "w")] public decimal Width { get; set; }
        [DataMember(Name = "h")] public decimal Height { get; set; }
    }

    [DataContract]
    public class BarcodeJob
    {
        [DataMember(Name = "data")] public string Data { get; set; }
        [DataMember(Name = "x")] public decimal X { get; set; }
        [DataMember(Name = "y")] public decimal Y { get; set; }
        [DataMember(Name = "w")] public decimal Width { get; set; }
        [DataMember(Name = "h")] public decimal Height { get; set; }
    }

    [DataContract]
    public class LabelJob
    {
        [DataMember(Name = "image")] public string Image { get; set; }
        [DataMember(Name = "qr")] public QrJob Qr { get; set; }
        [DataMember(Name = "barcode")] public BarcodeJob Barcode { get; set; }
        [DataMember(Name = "texts")] public List<TextJob> Texts { get; set; }
    }

    [DataContract]
    public class PrintJob
    {
        [DataMember(Name = "devicePath")] public string DevicePath { get; set; }
        [DataMember(Name = "copies")] public int Copies { get; set; }
        [DataMember(Name = "labels")] public List<LabelJob> Labels { get; set; }
    }

    [DataContract]
    public class Result
    {
        [DataMember(Name = "ok")] public bool Ok { get; set; }
        [DataMember(Name = "devices")] public List<string> Devices { get; set; }
        [DataMember(Name = "devicePath")] public string DevicePath { get; set; }
        [DataMember(Name = "state")] public string State { get; set; }
        [DataMember(Name = "printDes")] public string PrintDes { get; set; }
        [DataMember(Name = "errorMsg")] public string ErrorMsg { get; set; }
        [DataMember(Name = "error")] public string Error { get; set; }
        [DataMember(Name = "printedPages")] public int PrintedPages { get; set; }
        [DataMember(Name = "totalPages")] public int TotalPages { get; set; }
    }

    static class Program
    {
        static readonly List<string> TempImages = new List<string>();
        const string FontName = "\u5FAE\u8F6F\u96C5\u9ED1"; // 微软雅黑
        const string MsgNoDevice = "\u672A\u68C0\u6D4B\u5230\u7855\u65B9 T50/T80 \u6253\u5370\u673A\uFF0C\u8BF7\u68C0\u67E5 USB \u6570\u636E\u7EBF\u3001\u7535\u6E90\u548C\u9A71\u52A8";
        const string MsgNoStatus = "\u65E0\u6CD5\u8BFB\u53D6\u6253\u5370\u673A\u72B6\u6001";
        const string MsgNotReady = "\u6253\u5370\u673A\u4E0D\u5728\u5F85\u673A\u72B6\u6001";
        const string MsgNoLabels = "\u6CA1\u6709\u8981\u6253\u5370\u7684\u6807\u7B7E";
        const string MsgTimeout = "\u6253\u5370\u8D85\u65F6\uFF0C\u8BF7\u68C0\u67E5\u6253\u5370\u673A";

        static int Main(string[] args)
        {
            string outPath = args != null && args.Length > 0 ? args[args.Length - 1] : "";
            Result result;
            try
            {
                if (args == null || args.Length < 2)
                {
                    result = new Result();
                    result.Devices = new List<string>();
                    result.Ok = false;
                    result.Error = "usage: SupvanPrintHelper.exe status <out.json> | print <job.json> <out.json>";
                    WriteJson(outPath, result);
                    return 2;
                }

                string command = (args[0] ?? "").Trim().ToLowerInvariant();
                if (command == "status")
                {
                    result = GetStatus();
                }
                else if (command == "print" && args.Length >= 3)
                {
                    result = Print(args[1]);
                }
                else if (command == "validate" && args.Length >= 3)
                {
                    result = Validate(args[1]);
                }
                else
                {
                    result = new Result();
                    result.Devices = new List<string>();
                    result.Ok = false;
                    result.Error = "unknown command";
                }
            }
            catch (Exception ex)
            {
                result = new Result();
                result.Devices = new List<string>();
                result.Ok = false;
                result.Error = ex.ToString();
            }

            try
            {
                WriteJson(outPath, result);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex.ToString());
                return 3;
            }
            finally
            {
                CleanupImages();
            }
            return result.Ok ? 0 : 1;
        }

        static void CleanupImages()
        {
            foreach (string path in TempImages)
            {
                try
                {
                    if (File.Exists(path))
                    {
                        File.Delete(path);
                    }
                }
                catch (Exception)
                {
                    // Temp files are best-effort cleanup.
                }
            }
            TempImages.Clear();
        }

        static Result GetStatus()
        {
            Result result = new Result();
            result.Devices = new List<string>();
            try
            {
                List<string> paths = T50PROPrintUtil.GetDevicePaths();
                if (paths != null)
                {
                    result.Devices.AddRange(paths);
                }
                if (result.Devices.Count == 0)
                {
                    result.Ok = false;
                    result.Error = MsgNoDevice;
                    return result;
                }

                string devicePath = Environment.GetEnvironmentVariable("SUPVAN_PRINTER_DEVICE");
                if (string.IsNullOrWhiteSpace(devicePath))
                {
                    devicePath = result.Devices[0];
                }
                result.DevicePath = devicePath;

                PrintResult status = T50PROPrintUtil.GetPrintResult(devicePath);
                if (status == null)
                {
                    result.Ok = false;
                    result.Error = MsgNoStatus;
                    return result;
                }
                FillStatus(result, status);
                result.Ok = status.State == DeviceState.Waiting;
                if (!result.Ok)
                {
                    result.Error = StatusError(status);
                }
            }
            catch (Exception ex)
            {
                result.Ok = false;
                result.Error = ex.Message;
            }
            return result;
        }

        static Result Print(string jobPath)
        {
            Result result = new Result();
            result.Devices = new List<string>();
            try
            {
                PrintJob job = ReadJson<PrintJob>(jobPath);
                if (job == null || job.Labels == null || job.Labels.Count == 0)
                {
                    result.Ok = false;
                    result.Error = MsgNoLabels;
                    return result;
                }

                List<string> paths = T50PROPrintUtil.GetDevicePaths();
                if (paths != null)
                {
                    result.Devices.AddRange(paths);
                }
                if (result.Devices.Count == 0)
                {
                    result.Ok = false;
                    result.Error = MsgNoDevice;
                    return result;
                }

                string devicePath = job.DevicePath;
                if (string.IsNullOrWhiteSpace(devicePath))
                {
                    devicePath = Environment.GetEnvironmentVariable("SUPVAN_PRINTER_DEVICE");
                }
                if (string.IsNullOrWhiteSpace(devicePath))
                {
                    devicePath = result.Devices[0];
                }
                result.DevicePath = devicePath;

                PrintResult before = T50PROPrintUtil.GetPrintResult(devicePath);
                if (before == null)
                {
                    result.Ok = false;
                    result.Error = MsgNoStatus;
                    return result;
                }
                FillStatus(result, before);
                if (before.State != DeviceState.Waiting)
                {
                    result.Ok = false;
                    result.Error = StatusError(before);
                    return result;
                }

                SDKSPParamter parameter = BuildParameter(job);

                bool printed = false;
                Exception threadError = null;
                Thread thread = new Thread(delegate()
                {
                    try
                    {
                        printed = T50PROPrintUtil.DoPrint(parameter, devicePath);
                    }
                    catch (Exception ex)
                    {
                        threadError = ex;
                    }
                });
                thread.IsBackground = true;
                thread.Start();
                if (!thread.Join(TimeSpan.FromSeconds(150)))
                {
                    result.Ok = false;
                    result.Error = MsgTimeout;
                    return result;
                }
                if (threadError != null)
                {
                    result.Ok = false;
                    result.Error = threadError.Message;
                    return result;
                }

                result.Ok = printed;
                result.TotalPages = job.Labels.Count;
                try
                {
                    PrintResult after = T50PROPrintUtil.GetPrintResult(devicePath);
                    if (after != null)
                    {
                        FillStatus(result, after);
                        result.PrintedPages = after.DevPrintedPageCount;
                        result.TotalPages = after.PrintPageTotalCount > 0 ? after.PrintPageTotalCount : job.Labels.Count;
                    }
                }
                catch (Exception)
                {
                    // Status polling is best-effort; the print command itself already succeeded.
                }
                if (!result.Ok && IsPrintCompleted(result.PrintDes))
                {
                    result.Ok = true;
                }
                if (!result.Ok && result.TotalPages > 0 && result.PrintedPages >= result.TotalPages)
                {
                    result.Ok = true;
                }
                if (result.Ok)
                {
                    result.Error = null;
                }
                else if (string.IsNullOrWhiteSpace(result.Error))
                {
                    result.Error = !string.IsNullOrWhiteSpace(result.ErrorMsg)
                        ? result.ErrorMsg
                        : (!string.IsNullOrWhiteSpace(result.PrintDes) ? result.PrintDes : MsgNotReady);
                }
            }
            catch (Exception ex)
            {
                result.Ok = false;
                result.Error = ex.Message;
            }
            return result;
        }

        static Result Validate(string jobPath)
        {
            Result result = new Result();
            result.Devices = new List<string>();
            try
            {
                PrintJob job = ReadJson<PrintJob>(jobPath);
                if (job == null || job.Labels == null || job.Labels.Count == 0)
                {
                    result.Ok = false;
                    result.Error = MsgNoLabels;
                    return result;
                }
                BuildParameter(job);
                result.Ok = true;
                result.State = "Validated";
                result.TotalPages = job.Labels.Count;
                result.DevicePath = job.DevicePath;
            }
            catch (Exception ex)
            {
                result.Ok = false;
                result.Error = ex.Message;
            }
            return result;
        }

        static SDKSPParamter BuildParameter(PrintJob job)
        {
            SDKSPParamter parameter = new SDKSPParamter();
            parameter.PrintSet = new SDKPrintSet();
            parameter.PrintSet.Copy = job.Copies > 0 ? job.Copies : 1;
            parameter.PrintSet.Deepness = 4;
            parameter.PrintSet.DPI = 8f;
            parameter.PrintSet.Direction = 0;
            parameter.PrintSet.Gap = 2;
            parameter.PrintSet.Speed = 40;
            parameter.PrintSet.Width = 50;
            parameter.PrintSet.Height = 30;
            parameter.PrintSet.PaperType = 1;
            parameter.PrintSet.MaxDotValue = 384;
            parameter.PrintSet.OffsetH = 0;
            parameter.PrintSet.OffsetV = 0;
            parameter.PrintSet.OneByOne = true;
            parameter.PrintPages = new List<SDKPrintPage>();

            for (int i = 0; i < job.Labels.Count; i++)
            {
                LabelJob label = job.Labels[i];
                SDKPrintPage page = new SDKPrintPage();
                page.Repeat = 1;
                page.DrawObjects = new List<SDKPrintPageDrawObject>();

                if (!string.IsNullOrWhiteSpace(label.Image))
                {
                    byte[] bytes = Convert.FromBase64String(label.Image);
                    string imagePath = Path.Combine(
                        Path.GetTempPath(),
                        "partvault_label_" + Guid.NewGuid().ToString("N") + ".png");
                    File.WriteAllBytes(imagePath, bytes);
                    TempImages.Add(imagePath);

                    SDKPrintPageDrawObject image = new SDKPrintPageDrawObject();
                    image.X = 0m;
                    image.Y = 0m;
                    image.Width = 50m;
                    image.Height = 30m;
                    image.Content = imagePath;
                    image.FontName = FontName;
                    image.FontStyle = 0;
                    image.Align = 0;
                    image.FontSize = "0";
                    image.AutoReturn = false;
                    image.Format = "IMAGE";
                    page.DrawObjects.Add(image);
                    parameter.PrintPages.Add(page);
                    continue;
                }

                if (label.Qr != null && !string.IsNullOrWhiteSpace(label.Qr.Data))
                {
                    SDKPrintPageDrawObject qr = new SDKPrintPageDrawObject();
                    qr.X = label.Qr.X;
                    qr.Y = label.Qr.Y;
                    qr.Width = label.Qr.Width > 0 ? label.Qr.Width : 16.25m;
                    qr.Height = label.Qr.Height > 0 ? label.Qr.Height : 16.25m;
                    qr.Content = label.Qr.Data;
                    qr.FontName = FontName;
                    qr.FontStyle = 0;
                    qr.Align = 0;
                    qr.FontSize = "0";
                    qr.AutoReturn = false;
                    qr.Format = "QRCODE";
                    page.DrawObjects.Add(qr);
                }

                if (label.Barcode != null && !string.IsNullOrWhiteSpace(label.Barcode.Data))
                {
                    BarcodeJob source = label.Barcode;
                    SDKPrintPageDrawObject barcode = new SDKPrintPageDrawObject();
                    barcode.X = source.X;
                    barcode.Y = source.Y;
                    barcode.Width = source.Width;
                    barcode.Height = source.Height;
                    barcode.Content = source.Data;
                    barcode.FontName = FontName;
                    barcode.FontStyle = 0;
                    barcode.Align = 0;
                    barcode.FontSize = "0";
                    barcode.AutoReturn = false;
                    barcode.Format = "CODE_128";
                    page.DrawObjects.Add(barcode);
                }

                if (label.Texts != null)
                {
                    for (int j = 0; j < label.Texts.Count; j++)
                    {
                        TextJob text = label.Texts[j];
                        if (text == null || string.IsNullOrEmpty(text.Text))
                        {
                            continue;
                        }
                        SDKPrintPageDrawObject obj = new SDKPrintPageDrawObject();
                        obj.X = text.X;
                        obj.Y = text.Y;
                        obj.Width = text.Width;
                        obj.Height = text.Height;
                        obj.Content = text.Text;
                        obj.FontName = string.IsNullOrWhiteSpace(text.Font) ? FontName : text.Font;
                        obj.FontStyle = text.Bold ? 1 : 0;
                        obj.Align = text.Align;
                        obj.FontSize = string.IsNullOrWhiteSpace(text.Size) ? "2.2" : text.Size;
                        obj.AutoReturn = text.Wrap;
                        obj.Format = "TEXT";
                        page.DrawObjects.Add(obj);
                    }
                }

                parameter.PrintPages.Add(page);
            }
            return parameter;
        }

        static bool IsPrintCompleted(string description)
        {
            if (string.IsNullOrWhiteSpace(description))
            {
                return false;
            }
            string text = description.Trim();
            if (text.Contains("失败") || text.Contains("错误") || text.Contains("未完成"))
            {
                return false;
            }
            return text.Contains("已完成") || text.Contains("打印完成") || text.Contains("完成");
        }

        static string StatusError(PrintResult status)
        {
            if (status == null)
            {
                return MsgNoStatus;
            }
            if (!string.IsNullOrWhiteSpace(status.ErrorMsg))
            {
                return status.ErrorMsg;
            }
            if (!string.IsNullOrWhiteSpace(status.PrintDes))
            {
                return status.PrintDes;
            }
            return MsgNotReady;
        }

        static void FillStatus(Result result, PrintResult status)
        {
            result.State = status.State.ToString();
            result.PrintDes = status.PrintDes;
            result.ErrorMsg = status.ErrorMsg;
            result.PrintedPages = status.DevPrintedPageCount;
            result.TotalPages = status.PrintPageTotalCount;
        }

        static T ReadJson<T>(string path)
        {
            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(T));
            using (FileStream stream = File.OpenRead(path))
            {
                return (T)serializer.ReadObject(stream);
            }
        }

        static void WriteJson<T>(string path, T value)
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                throw new ArgumentException("output path is required");
            }
            DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(T));
            using (FileStream stream = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                serializer.WriteObject(stream, value);
            }
        }
    }
}
