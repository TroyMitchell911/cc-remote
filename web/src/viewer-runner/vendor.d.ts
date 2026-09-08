declare module "parse-srcset" {
  export default function parseSrcset(source: string): { url: string; w?: number; h?: number; d?: number }[];
}
